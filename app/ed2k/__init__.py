"""ed2k 领域包：哈希计算（纯函数）。

- hasher.py  ed2k 链接生成（MD4 Merkle 分块哈希）

流水线的哈希/推送/上传阶段统一在 app/pipeline/service.py（方案二整合）；
providers/ed2k.py（Ed2kProvider）为 provider 协议层的薄适配，归属集成层。
"""
