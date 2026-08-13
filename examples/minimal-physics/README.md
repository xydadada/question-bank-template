# 最小结构示例

[English](#english)

这个目录只解释模板会把资料整理成什么样子，不会自动上传到 WeKnora，也不会调用
MinerU 或 MiMo。题目和解答均为本仓库编写的虚构样例，不来自真实试卷或教材。

输入分成两个文件：

- [`source/question.md`](source/question.md) 是题目；
- [`source/answer.md`](source/answer.md) 是解答。

整理后可以同时得到：

- [`expected/parent.md`](expected/parent.md)：一道完整题目及其答案；
- [`expected/child.md`](expected/child.md)：适合检索的题答单元；
- [`expected/raw.md`](expected/raw.md)：保留来源顺序的完整文字。

实际文件会包含程序生成的 `group_id`、摘要、文档 ID 和更多处理状态。这里删去了这些
运行时字段，只保留三层结构和检索时可见的分类信息。这些文件用于说明输出格式，逐字节
回归由自动化测试负责。

可以用下面的查询理解三层索引各自的作用：

```text
弹簧振子的角频率和总能量
```

子块负责直接命中题目，父块返回完整题答上下文，原文层保留输入资料的整体顺序。

## English

This directory shows the shape of the three index layers without calling MinerU, MiMo, or
WeKnora. The question and solution are synthetic and were written for this repository.

The two source files become a complete parent record, a focused child record, and a raw
record that preserves source order. Real output also contains generated IDs, digests, and
processing state. These examples omit runtime fields and explain the output format. Automated
tests cover byte-level regression checks.

Try reading the three expected files as the results of this query:

```text
spring oscillator angular frequency and total energy
```
