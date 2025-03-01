import os
import time
from copy import deepcopy
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Subset
from torchvision.datasets import ImageFolder
from torchvision import transforms
from torchvision.models import efficientnet_v2_s
from torch.cuda.amp import autocast, GradScaler
import numpy as np
from sklearn.model_selection import train_test_split
from torch.utils.tensorboard import SummaryWriter


writer = SummaryWriter(log_dir='runs/dog_breed_experiment_10')
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'max_split_size_mb:128'  # 防止内存碎片
# ==============================
# 数据增强及预处理
# ==============================
transform_train = transforms.Compose([
    transforms.RandomResizedCrop(224, scale=(0.5, 1.0)),  # 扩大裁剪范围
    transforms.RandomHorizontalFlip(p=0.6),
    transforms.RandomVerticalFlip(p=0.3),
    transforms.ColorJitter(brightness=0.5, contrast=0.5, saturation=0.4),
    transforms.RandomAffine(degrees=20, translate=(0.15, 0.15)),
    transforms.RandomApply([transforms.GaussianBlur(5)], p=0.4),
    transforms.RandomSolarize(threshold=128, p=0.2),  # 新增光斑效果
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    transforms.RandomErasing(p=0.4, scale=(0.03, 0.15), value='random')  # 随机颜色擦除
])

transform_test = transforms.Compose([
    transforms.Resize(256),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
])


class ModelEMA:
    def __init__(self, model, decay=0.999, total_epochs=50):
        self.ema = deepcopy(model).eval()
        self.initial_decay = decay  # 初始衰减率
        self.min_decay = 0.995      # 最低衰减率
        self.total_epochs = total_epochs
        for param in self.ema.parameters():
            param.requires_grad_(False)

        # 初始参数同步
        with torch.no_grad():
            for ema_p, model_p in zip(self.ema.parameters(), model.parameters()):
                ema_p.copy_(model_p)

    def update(self, model, current_epoch):
        # 线性衰减策略：从initial_decay降到min_decay
        decay = self.initial_decay - (self.initial_decay - self.min_decay) * (current_epoch / self.total_epochs)
        decay = max(decay, self.min_decay)  # 确保不低于最小值
        with torch.no_grad():
            for ema_param, model_param in zip(self.ema.parameters(), model.parameters()):
                # 处理半精度参数
                model_param_fp32 = model_param.detach().float()
                ema_param_fp32 = ema_param.float()
                ema_param_fp32.mul_(decay).add_(model_param_fp32, alpha=1 - decay)
                ema_param.copy_(ema_param_fp32)


# ==============================
# 加载 Stanford Dogs 数据集（Kaggle版本）
# ==============================
data_dir = './images/images'  # 请将数据集解压后的文件夹路径填写在此处

# 分别创建两个 ImageFolder 实例，分别设置训练和验证时的变换
full_dataset_train = ImageFolder(root=data_dir, transform=transform_train)
full_dataset_val   = ImageFolder(root=data_dir, transform=transform_test)

# 获取所有样本标签用于分层划分
all_targets = full_dataset_train.targets

# 使用 train_test_split 进行分层划分，80%作为训练集，20%作为验证集
train_idx, val_idx = train_test_split(
    list(range(len(all_targets))),
    test_size=0.2,
    stratify=all_targets
)

train_set = Subset(full_dataset_train, train_idx)
val_set   = Subset(full_dataset_val, val_idx)

print(f"训练集: {len(train_set)} 个样本, 验证集: {len(val_set)} 个样本")

# ==============================
# 定义网络模型（以 efficientnet_v2_s 为例）
# ==============================
def get_net(devices):
    model = efficientnet_v2_s(weights='DEFAULT')

    # 冻结stem和前4个block
    for param in model.features[:4].parameters():
        param.requires_grad = False

    # 修改分类头（保持特征维度）
    model.classifier = torch.nn.Sequential(
        torch.nn.Dropout(p=0.5),  # 增加Dropout比例
        torch.nn.Linear(1280, 1024),
        torch.nn.BatchNorm1d(1024),
        torch.nn.SiLU(inplace=True),
        torch.nn.Dropout(p=0.3),
        torch.nn.Linear(1024, 512),
        torch.nn.LayerNorm(512),  # 改用LayerNorm
        torch.nn.SiLU(inplace=True),
        torch.nn.Linear(512, 120)
    )
    return model.to(devices)

pretrained_net = get_net(device)

# ==============================
# 定义优化器和学习率调度器
# ==============================
# 修改优化器参数
optimizer = torch.optim.AdamW(
    [
        {'params': pretrained_net.features[4:].parameters(), 'lr': 2e-5, 'weight_decay': 0.001},
        {'params': pretrained_net.classifier[:-4].parameters(), 'lr': 5e-4, 'weight_decay': 0.003},
        {'params': pretrained_net.classifier[-4:].parameters(), 'lr': 1e-3, 'weight_decay': 0.005}
    ],
    betas=(0.95, 0.999),
    eps=1e-8  # 增加数值稳定性
)

# 修改调度器参数
plateau_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
    optimizer,
    mode='max',
    factor=0.3,     # 更温和的衰减幅度
    patience=3,     # 缩短观察窗口
    threshold=0.001 # 更敏感的阈值
)

cosine_scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
    optimizer,
    T_0=12,        # 延长余弦周期到12个epoch
    T_mult=2,       # 周期倍增
    eta_min=1e-6
)

# ==============================
# 定义评价函数
# ==============================
def evaluate_accuracy(data_iter, net, loss_fn, device=None, use_ema=False, ema_model=None):
    """
    计算验证集的准确率和损失
    use_ema: 是否使用EMA模型
    ema_model: EMA模型实例
    """
    if device is None and isinstance(net, nn.Module):
        device = next(net.parameters()).device

    # 选择模型
    if use_ema:
        if ema_model is None:
            raise ValueError("ema_model must be provided when use_ema is True")
        model = ema_model.ema
    else:
        model = net

    model.eval()

    acc_sum, loss_sum, n = 0.0, 0.0, 0
    with torch.no_grad():
        for X, y in data_iter:
            X, y = X.to(device), y.to(device)
            with autocast():
                output = model(X)
                loss = loss_fn(output, y)
            acc_sum += (output.argmax(dim=1) == y).float().sum().item()
            loss_sum += loss.item() * y.size(0)
            n += y.shape[0]
    return acc_sum / n, loss_sum / n




def train(train_iter, test_iter, net, loss, optimizer, device, num_epochs,
          best_acc=0.0, accum_steps=4):
    best_metric = 0.0
    patience = 12
    no_improve = 0
    net = net.to(device)
    scaler = GradScaler()
    ema = ModelEMA(net)
    # 记录当前使用的调度器
    current_scheduler = "cosine"
    print(f'training on {device} with accum_steps={accum_steps}')

    for epoch in range(num_epochs):
        net.train()
        train_l_sum, train_acc_sum, n = 0.0, 0.0, 0
        epoch_start = time.time()
        total_batches = len(train_iter)

        # 初始化进度跟踪变量
        batch_times = []
        progress_bar_length = 30

        for batch_idx, (X, y) in enumerate(train_iter):
            batch_start = time.time()
            X, y = X.to(device), y.to(device)

            # 混合精度前向
            with autocast():
                y_hat = net(X)
                l = loss(y_hat, y) / accum_steps

            unscaled_loss = l.detach().clone() * accum_steps
            scaler.scale(l).backward()

            # 梯度累积条件判断
            if (batch_idx + 1) % accum_steps == 0 or (batch_idx + 1) == total_batches:
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(net.parameters(), max_norm=2.0)  # 添加梯度裁剪
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()
                ema.update(net,epoch)

            # 统计指标
            batch_acc = (y_hat.argmax(dim=1) == y).sum().item() / y.size(0)
            train_l_sum += unscaled_loss.item() * y.size(0)
            train_acc_sum += (y_hat.argmax(dim=1) == y).sum().item()
            n += y.size(0)

            # 计算进度和时间预估
            batch_time = time.time() - batch_start
            batch_times.append(batch_time)
            avg_batch_time = np.mean(batch_times[-10:])

            # 进度计算
            completed = batch_idx + 1
            progress = completed / total_batches
            elapsed = time.time() - epoch_start
            remaining = avg_batch_time * (total_batches - completed)

            # 构建进度条
            filled_length = int(progress_bar_length * progress)
            progress_bar = '█' * filled_length + '-' * (progress_bar_length - filled_length)

            # 实时显示信息
            info = (f"Epoch {epoch + 1}/{num_epochs} |{progress_bar}| "
                    f"{completed}/{total_batches} batches "
                    f"[{elapsed:.0f}s<{remaining:.0f}s, {1 / avg_batch_time:.1f}batches/s] "
                    f"Loss: {unscaled_loss.item():.4f} Acc: {batch_acc:.4f}")
            print("\r" + info, end="")

        # 完成一个epoch后换行
        epoch_time = time.time() - epoch_start
        print(f"\rEpoch {epoch + 1} completed in {epoch_time:.1f}s".ljust(120))

        # 验证阶段
        test_acc, test_loss = evaluate_accuracy(test_iter, net, loss, device, use_ema=False, ema_model=ema)
        print(f"  Train Loss: {train_l_sum / n:.4f}  Train Acc: {train_acc_sum / n:.4f}")
        print(f"  Val Loss (Original): {test_loss:.4f}  Val Acc (Original): {test_acc:.4f}")
        ema_test_acc, ema_test_loss = evaluate_accuracy(test_iter, net, loss, device, use_ema=True, ema_model=ema)
        print(f"  Val Loss (EMA): {ema_test_loss:.4f}  Val Acc (EMA): {ema_test_acc:.4f}")

        # 动态切换调度策略
        if epoch < 12:  # 前12个epoch使用cosine
            cosine_scheduler.step(epoch)  # 需要传入当前epoch
            current_scheduler = "cosine"
        else:  # 第12个epoch之后使用plateau
            if epoch == 12:
                # 将plateau的基准学习率设为当前值
                for i, group in enumerate(optimizer.param_groups):
                    group['initial_lr'] = group['lr']
                plateau_scheduler.base_lrs = [group['lr'] for group in optimizer.param_groups]
            plateau_scheduler.step(test_acc)
            current_scheduler = "plateau"
            # 打印学习率信息（调试用）
        current_lrs = [f"{g['lr']:.2e}({current_scheduler})" for g in optimizer.param_groups]
        print(f"  Current lrs: {current_lrs}\n")
        # 记录到TensorBoard
        writer.add_scalar('Loss/train', train_l_sum / n, epoch)
        writer.add_scalar('Accuracy/train', train_acc_sum / n, epoch)
        writer.add_scalar('Loss/val', test_loss, epoch)  # 新增验证损失
        writer.add_scalar('Accuracy/val', test_acc, epoch)
        writer.add_scalar('Metrics/LR_Group0', optimizer.param_groups[0]['lr'], epoch)
        writer.add_scalar('Metrics/LR_Group1', optimizer.param_groups[1]['lr'], epoch)
        writer.add_scalar('Metrics/TrainVal_Gap', train_acc_sum / n - test_acc, epoch)  # 过拟合指标
        writer.add_scalar('Loss/val_ema', ema_test_loss, epoch)
        writer.add_scalar('Accuracy/val_ema', ema_test_acc, epoch)

        # 模型保存逻辑
        if test_acc > best_acc:
            best_acc = test_acc
            print(f"  New best accuracy! Saving the model...")
            torch.save(net.state_dict(), 'best_model.pth')
            # 保存最佳EMA模型
        if ema_test_acc > best_acc:
            best_acc = ema_test_acc
            print(f"  New best ema accuracy! Saving the ema model...")
            torch.save(ema.ema.state_dict(), 'best_model_ema.pth')

        # 使用加权指标（准确率为主，loss为辅）
        current_metric = test_acc * 0.8 + (1 - test_loss) * 0.2
        if current_metric > best_metric:
            best_metric = current_metric
            no_improve = 0
        else:
            no_improve += 1
            if no_improve >= patience:
                print(f"早停触发：连续{patience}个epoch综合指标无提升")
                break

        # 打印学习率信息
        current_lrs = [f"{g['lr']:.2e}" for g in optimizer.param_groups]
        print(f"  Current lrs: {current_lrs}\n")

    # 关闭TensorBoard Writer
    writer.close()
    return best_acc


def train_fine_tuning(net, optimizer, batch_size=64, num_epochs=50):
    train_iter = DataLoader(
        train_set,
        batch_size,
        shuffle=True,
        pin_memory=True,
    )
    val_iter = DataLoader(
        val_set,
        batch_size,
        shuffle=False,
        pin_memory=True,
    )
    loss_fn = nn.CrossEntropyLoss(label_smoothing=0.15)
    best_acc = train(train_iter, val_iter, net, loss_fn, optimizer, device, num_epochs)
    return best_acc

if __name__ == '__main__':
    train_fine_tuning(pretrained_net, optimizer)
