# elective-scout

你去 Quest 想加一门选修，结果发现：

- 先修条件不满足，rejected。
- 换一门，时间和已有课冲突，rejected。
- 再换一门，发现是 Cambridge 或 St. Jerome's 校区，不想跑那么远，rejected。
- 又换一门，全在线上，不符合你的需求，rejected。

每次都要先查 catalog 看能不能选，再去 classes.uwaterloo.ca 一条条翻 section 的时间，再手动对比你的课表。课程多的时候，这个过程可以耗掉一个下午。

这个工具把这三件事一次性自动完成：**哪些课的先修条件你已经满足了、这学期在哪开课、和你已注册的课有没有时间冲突**。跑完之后你拿到的是一张过滤好的清单，而不是一个要自己从头筛的课程表。

---

## 运行

```bash
python elective_scout.py
```

脚本会依次问你：

1. **专业** — 输入关键词（如 `electrical`、`software`、`computer eng`），从匹配列表中选择
2. **当前学期**（如 `3A`）
3. **此前完成的选修课**（若没有直接回车）
4. **本学期额外注册的课程**（必修课以外；若没有直接回车）
5. **课表学期**（如 `W26`、`S26`、`F26`；回车使用当前学期）

当前学期的必修课从 catalog 自动推算，**无需手动输入**。

**默认输出** — 直接在终端打印可选课的课程号：

```
Eligible electives — spring2026

online (3):
  ANTH101  CS449  PSYCH100

no conflict (9):
  ECE302  ECE405  ECE414  ECE488  ECE493  MTE546  SYDE522  SYDE556  STAT441
```

加 `--verbose` 同时显示课程名。加 `--report` 输出完整分类文件到磁盘。

---

## 它怎么工作

**第一步：先修条件分类**

从 Kuali catalog 拉下来你专业的所有选修课，对每一门走一遍先修条件树，判断最早哪个学期你能选。结果分成 `2B / 3A / 3B / impossible`。

"impossible" 的判断是个人化的：你当前 standing 之前的所有学期必修课都自动算作已完成；你提供的此前选修课用于处理以选修为先修的情况。

**第二步：课表查询**

对非 `impossible` 的课程，查询所选学期的开课信息，按地点分类：

- `online`：全线上
- `UW U`：有线下 section，且线下全在 UW 主校区
- `other`：有线下 section，但不在 UW 主校区
- `n/a`：这学期没开

**第三步：排课冲突检查**

对 `UW U` 的课程，自动拉取当前学期必修课的时间表，构建已占用时间块，对每门选修标出 `no conflict` 或 `with conflict`。

---

## 输入格式

已注册课程支持三种格式，用逗号或分号分隔，一行输完：

```
MATH239, ECE250 LEC 001, ECE316:LEC 001
```

只写课程号（如 `MATH239`）时，该课所有 section 时间均视为已占用（保守判断）。

---

## 高级 / 脚本化用法

所有交互提示都可以用 flag 跳过：

| flag | 说明 |
|---|---|
| `--non-interactive` | 禁用所有提示，完全由 flag 驱动 |
| `--catalog-id` | Kuali catalog ID |
| `--program-pid` | 专业页面 PID |
| `--program-name` | 专业全名 |
| `--schedule-term` | 学期别名或代码（如 `W26`、`S26`、`1265`）|
| `--output-dir` | 输出目录（默认：当前目录）|
| `--output-prefix` | 文件前缀（默认：从专业名派生）|
| `--report` | 将分类和课表文件写入磁盘 |
| `--verbose` | 在终端输出中显示课程名 |
| `--student-major-name` | 专业全名 |
| `--student-standing` | 学期（如 `3A`）|
| `--student-completed-course`（可多次）| 已完成选修课 |
| `--student-registered-course`（可多次）| 本学期额外注册课程 |
| `--allowed-marker`（可多次）| 追加视为满足的 prerequisite 文本片段 |
