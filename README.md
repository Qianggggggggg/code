# EfficientNetV2 Dog Breed Classification 

基于EfficientNetV2的犬种细粒度分类系统，实现Stanford Dogs数据集的120类精准识别。创新性融合EMA模型、动态学习率调度与混合精度训练，验证集准确率达**93.22%**。

##  核心特性

- **智能训练优化**
  -  动态EMA衰减策略（0.999 → 0.995）
  -  分层学习率配置（2e-5 ~ 1e-3）
  -  AMP混合精度训练 + 梯度累积

- **鲁棒数据增强**
  -  10种增强组合（随机擦除/仿射变换/光斑效果等）
  -  改进的随机裁剪策略（scale=0.5-1.0）

- **可视化监控**
  -  TensorBoard实时追踪指标
  -  学习率/梯度分布/过拟合分析

 ##实验记录如下
  -  训练集：https://github.com/Qianggggggggg/code/blob/main/record/train.png
  -  测试集：https://github.com/Qianggggggggg/code/blob/main/record/val.png
  -  测试集（ema）：https://github.com/Qianggggggggg/code/blob/main/record/val_ema.png
