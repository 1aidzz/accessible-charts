# Chartability 映射表 —— 目标一工作面定义

> 生成：2026-09-29 ｜ **Phase 1 产出（读标准 + 裁剪，未写代码）**>   
> 依据：Chartability workbook v2（POUR+CAF，7 原则 / 50 条启发式 / 14 条 critical，CC-BY-SA 3.0）>   
> Elavsky, Bennett, Moritz. *How accessible is my visualization? Evaluating visualization accessibility with Chartability.* EuroVis 2022.>   
> 适用对象：本地 LLM 生成的**静态 matplotlib → SVG** 图（无交互）

---

## 0. 三十秒速读

| 层               | 数量                   | 含义                                    |
| --------------- | -------------------- | ------------------------------------- |
| Chartability 全集 | 50（14 critical）      | 原始标准                                  |
| **MAIN 主判定集**   | **13 条（7 critical）** | **进 CPR 分母**——本报告所有"合规率"以此为准          |
| MERGE 合并项       | 3 条                  | 与 MAIN 语义重叠，不重复计数                     |
| WATCH 观察项       | 10 条                 | 判据主观 / 需 VLM / 条件适用 / 批次级，单独统计不进分母    |
| AUTO-PASS       | 2 条                  | 因"静态矢量图"而技术性天然满足，记录理由                 |
| OUT 剔除项         | 22 条                 | 交互与网页环境类，静态图无此概念 → **写明为 limitation** |

三个必须记住的判断：

1. **22 条被剔除不是偷懒，是作用域声明。** 其中 14 条属于 Operable / 网页环境（键盘、焦点、滚动、动画）。我们的产物是静态图，报告里必须写清楚："本 benchmark 覆盖静态图无障碍，交互维度不在范围内。"
2. **真正能自动判定且值得计分的只有 13 条**，其中 7 条 critical：对比度、非视觉可达、字号、目的说明、标题摘要、阅读等级、数据表。
3. **由此派生出两项工程改造**（见 §3），其中第二条会直接影响实验设计的公平性。

---

## 1. 标签体系（三条 ACE）

每条启发式都打三个标签——这就是映射表的全部方法论：

| 标签         | 取值                                   | 含义                                          |
| ---------- | ------------------------------------ | ------------------------------------------- |
| **A 适用性**  | MAIN / MERGE / WATCH / AUTO / OUT    | 进不进判定集、怎么算                                  |
| **C 检查方式** | RULE / GEO / CVD / NLP / VLM / HUMAN | 谁来判：确定性规则 / 几何 / 色觉仿真 / 文本度量 / 视觉模型 / 人工    |
| **E 修复杠杆** | P / R / B / —                        | PROMPT（生成前约束）/ REPAIR（失败原因回传重生成）/ BOTH / 不修 |

---

## 2. 主表：50 条逐条映射

图例：`★` = Chartability 标记的 critical；检查方式见上；杠杆 P=提示词约束，R=自修复反馈，B=两者并用。

### Perceivable 可感知（8 条 · 4 critical）

| #  | 启发式（原文失效名）                                 | ★ | A     | C           | 判据（可执行）                                                                         | E |
| -- | ------------------------------------------ | - | ----- | ----------- | ------------------------------------------------------------------------------- | - |
| P1 | Low contrast                               | ★ | MAIN  | RULE        | 文本≥4.5:1、几何/大文本≥3:1（WCAG 相对亮度）；取 SVG `fill`/`stroke` × 画布背景                     | B |
| P2 | Content is only visual                     | ★ | MAIN  | RULE + 人工抽检 | SVG 含 `role="img"` + `<title>` + `<desc>` **且**附数据表/文字摘要；真机屏幕阅读器验证留到 Phase 4 抽样 | B |
| P3 | Small text size                            | ★ | MAIN  | RULE        | 所有 `<text>` 的 `font-size` ≥12px（9pt）。**matplotlib 默认 10px → 天然违规**              | B |
| P4 | Visual presents seizure risk               | ★ | AUTO  | 常识          | 静态无动画、无红闪 → 默认通过，报告注明依据                                                         | — |
| P5 | Color is used alone to communicate meaning |   | WATCH | RULE + VLM  | 多类编码时必须带第二通道（marker / 线型 / hatch / 直接标注）                                        | B |
| P6 | Meaningful elements can't be distinguished |   | MAIN  | GEO / VLM   | 相邻元素间隙≥1px、文字包围盒互不重叠（**复用 viseval 自带 `overlapScript`**）                         | R |
| P7 | Not CVD-friendly                           |   | MAIN  | CVD         | protan/deutan/tritan 仿真后编码色两两 ΔE2000 ≥ 阈值，或命中安全调色板白名单（Okabe-Ito / viridis）      | B |
| P8 | Spacing is inappropriate                   |   | WATCH | VLM         | 留白 / 柱间距比例（主观性强）                                                                | P |

### Operable 可操作（8 条 · 3 critical）→ **全部 OUT**

| #  | 启发式                                             | ★ | 处置                                      |
| -- | ----------------------------------------------- | - | --------------------------------------- |
| O1 | Interaction modality only has one input type    | ★ | OUT：静态图无交互。改 HTML/Altair 路线后需重新纳入（见 §7） |
| O2 | No interaction cues or instructions             | ★ | OUT：同上                                  |
| O3 | Controls override AT controls                   | ★ | OUT：同上                                  |
| O4 | Low contrast on interactive elements            |   | OUT：无交互态                                |
| O5 | Keyboard focus indicator missing / low contrast |   | OUT：无可聚焦元素                              |
| O6 | Inappropriate tab stops                         |   | OUT：无 tab 停靠                            |
| O7 | Complex actions have no alternatives            |   | OUT：无刷选/缩放                              |
| O8 | Target pointer interaction size is too small    |   | OUT：无指针目标                               |

> 这 8 条（含 3 条 critical）被整体排除，是本 benchmark **最大的一处 limitation**，必须在报告的限制章节显式声明，而不是悄悄略过。

### Understandable 可理解（10 条 · 3 critical）

| #   | 启发式                                                | ★ | A               | C         | 判据（可执行）                                               | E |
| --- | -------------------------------------------------- | - | --------------- | --------- | ----------------------------------------------------- | - |
| U1  | No explanation for purpose or for how to read      | ★ | MAIN            | RULE（存在性） | 输出含"这张图怎么看 / 想说明什么"的说明字段（依赖输出契约扩展，见 §3a）              | B |
| U2  | No title, summary, or caption                      | ★ | MAIN            | RULE      | 标题文本节点非空且非默认名（如 `y vs x`）；鼓励结论式标题                     | B |
| U3  | Reading level inappropriate                        | ★ | MAIN            | NLP       | `textstat` Flesch–Kincaid Grade ≤9（VisEval 为英文语料）     | B |
| U4  | Interactive context is not clear                   |   | OUT             | —         | 无联动控件                                                 | — |
| U5  | Information complexity is inappropriate            |   | WATCH（advisory） | RULE      | 类别数≤5、无双轴、无 z 轴编码 —— **与 legality 冲突，只统计不判分**（见 §5.1） | — |
| U6  | Changes are not easy to follow                     |   | OUT             | —         | 无动画/状态变化                                              | — |
| U7  | Metrics and variables are undefined                |   | MAIN            | RULE      | 轴标签含变量名/单位，非纯数字、非裸列名                                  | B |
| U8  | Statistical uncertainty isn't clearly communicated |   | WATCH（条件）       | RULE      | 仅当图含误差棒/置信区间时，检查有无文字解释                                | — |
| U9  | Axis labels are unclear or missing                 |   | MAIN            | RULE      | x/y 标签均存在且可读；**截断轴须显式标注**                             | R |
| U10 | Controls are inappropriate                         |   | OUT             | —         | 无控件                                                   | — |

### Robust 健壮（3 条 · 0 critical）

| #  | 启发式                           | ★ | A               | C    | 判据                                      | E |
| -- | ----------------------------- | - | --------------- | ---- | --------------------------------------- | - |
| R1 | Does not conform to standards |   | WATCH（umbrella） | —    | 作为报告口径声明；具体 Compliance 由各 MAIN 条代理      | — |
| R2 | Semantically invalid          |   | MERGE → P2      | RULE | 结构语义检查并入 P2 的 role/title/desc 判定，避免重复计数 | B |
| R3 | Fragile technology support    |   | MERGE → C1      | RULE | "多技术路径可达"退化为 SVG + CSV 并存，并入 C1         | B |

### Compromising 兼容（7 条 · 1 critical）

| #  | 启发式                                                    | ★ | A               | C    | 判据                                        | E |
| -- | ------------------------------------------------------ | - | --------------- | ---- | ----------------------------------------- | - |
| C1 | No table                                               | ★ | MAIN            | RULE | 输出含结构化数据表（**CSV 文件优先**；采用保守口径，不豁免，见 §5.2） | B |
| C2 | Information can only be reached through single process |   | OUT             | —    | 需搜索/并行 UI 的应用环境                           | — |
| C3 | Location and history is unclear                        |   | OUT             | —    | 仪表盘环境                                     | — |
| C4 | Interactions are not forgivable                        |   | OUT             | —    | 需撤销重做                                     | — |
| C5 | Information cannot be navigated by narrative/structure |   | MERGE → P2 + C1 | —    | 静态下退化为"标题 → 描述 → 表格"顺序，由 P2+C1 覆盖         | — |
| C6 | Table/data is static                                   |   | WATCH           | RULE | 表格须满足可下载 / 可排序 / 可过滤之一；产出独立 CSV 即满足"可下载"  | P |
| C7 | State is not easy to share and reproduce               |   | OUT             | —    | 无自定义视图状态                                  | — |

### Assistive 减负（6 条 · 2 critical）

| #  | 启发式                                          | ★ | A     | C          | 判据                                                                             | E |
| -- | -------------------------------------------- | - | ----- | ---------- | ------------------------------------------------------------------------------ | - |
| A1 | Data density is inappropriate                | ★ | WATCH | RULE + VLM | marks 数 / 绘图面积超阈值（如 >50）时须聚合或解释。**critical 但判据主观 → pilot 后决定是否升 MAIN**（见 §5.3） | R |
| A2 | Navigation and interaction is tedious        | ★ | OUT   | —          | 静态无交互；表格已大幅降低导航成本                                                              | — |
| A3 | Visually apparent features are not described |   | MAIN  | RULE（存在性）  | 输出含趋势 / 极值 / 异常的文字描述 → **★ alt text 的天然接口，直接接目标二 L2**                          | B |
| A4 | Data in text is not human-readable           |   | MAIN  | RULE       | 刻度标签数值人类可读：无科学计数法、小数位合理、大数缩写（6500000000 → 6.5b）                                | R |
| A5 | Space does not handle extremes               |   | WATCH | RULE + VLM | 极值比过大时是否做了处理/注释                                                                | P |
| A6 | No default "build-your-own" provided         |   | OUT   | —          | 无自助探索界面                                                                        | — |

### Flexible 灵活（8 条 · 1 critical）

| #  | 启发式                                       | ★ | A               | C    | 判据                                                             | E |
| -- | ----------------------------------------- | - | --------------- | ---- | -------------------------------------------------------------- | - |
| F1 | User style change not respected           | ★ | OUT（limitation） | —    | 静态 SVG 无法支持用户样式覆盖 → 写入 limitation（workbook 明确警告静态设计在 C/F 类风险高） | — |
| F2 | Long animations cannot be controlled      |   | OUT             | —    | 无动画                                                            | — |
| F3 | Scrolling experiences cannot be altered   |   | OUT             | —    | 无滚动叙事                                                          | — |
| F4 | Zoom and reflow are not supported         |   | AUTO            | —    | **矢量 SVG 天然支持缩放与重排** → 默认通过                                    | — |
| F5 | User's text adjustments are not respected |   | OUT（limitation） | —    | 同 F1                                                           | — |
| F6 | Design is not consistent and familiar     |   | WATCH（批次级）      | RULE | 批次内调色板 / 字号 / 字体一致性——跨图统计，不是单图指标                               | P |
| F7 | Contrast and textures cannot be adjusted  |   | OUT（limitation） | —    | 静态不可调                                                          | — |
| F8 | Difficult chart type has no alternative   |   | WATCH           | RULE | 饼图等高风险图型须提供替代呈现或数据直标 → **★ 与老师的"多样化"衔接：E 组消融让模型在无障碍约束下自选图型**   | P |

---

## 3. 由此派生的两项工程改造（重要）

### (a) Agent 的输出契约必须扩展

P2、U1、U2、A3、C1 这 5 条 MAIN 里，**有 4 条判的是"图以外的东西"**（数据表、读出说明、结论标题、趋势描述）。如果 agent 只吐一段 matplotlib 代码，这 4 条永远判挂——CPR 会被系统性低估，而且模型再怎么修也无解。

→ 新契约：agent 输出 `{code, title, readout_note, takeaway, data_table_csv}`（代码 + 三件套）。  
parse 不出这些字段，就判这几条失败——这本身可判定，不会漏计。

### (b) 「后处理注入」vs「模型自生成」必须分开对照

`<title>/<desc>` 和 CSV 表格，**我们完全可以事后替模型注入**（改几行后处理即可，100% 通过）。但那样测的就是我们而不是模型。

→ 实验必须分两组：

- `generated` 组：要求模型自己产出三件套（真实能力，主考察对象）
- `injected` 组：后处理兜底（作为**上界参考**）

这组对照本身就是报告里的一张牌——和之前"修正官方只读首表口径"那张牌同一类型：**指出评测口径本身的差异有多大**。

---

## 4. 指标定义：CPR（Chartability Pass Rate）

设某张图适用 $n$ 条 MAIN 项，其中判定通过 $k$ 条，则

```
CPR_instance = k / n
```

**三条硬规矩（针对 viseval 的 `all([])==True` 坑）**：

1. **不适用必须从分子分母同时剔除**，并单独记 `"status": "n/a" + 理由`。**绝不能默认通过**。     
   例：单序列图没有颜色区分类别 → P7 记 n/a，不进分母。
2. **$n = 0$ 时该图不计入均值**，另作统计（`coverage` 字段），不能当 100 分。
3. **只在 validity 通过的图上判定**（与 readability 一样的级联短路）；因此必须同时报告判定覆盖率，否则 CPR 可以靠"少判图"作弊。

**三个层次的数**：

| 指标                        | 分母                 | 用途                                              |
| ------------------------- | ------------------ | ----------------------------------------------- |
| `CPR_all`                 | 13 条 MAIN 的适用子集    | 主指标                                             |
| `CPR_critical`            | 7 条 critical 的适用子集 | 对应 Chartability 官方"0 critical"底线目标，**最容易讲的一句话** |
| `failure_rate[heuristic]` | 单条 / 适用数           | 定位"哪条最难修"，画失败分布图                                |

---

## 5. 四个必须先定的口径（写死在代码注释里）

1. **U5 信息复杂度 ≤5 类 vs legality 全量数据 —— 冲突。**     
   VisEval 要求画出全部数据，Chartability 建议超过 5 类就聚合/拆图，两者不可兼得。     
   → **定为 advisory：只统计不入分母**，并在报告里主动讨论这个矛盾（这是很有价值的发现，不是瑕疵）。
2. **P2/C1 的豁免条款不采用。** 官方说"若标题/摘要已足够传达全部信息可免数据表"，我们使用**保守口径一律要表**，避免主观豁免导致的分数虚高。
3. **A1 数据密度是 critical，但判据主观，先放 WATCH。** Phase 2 跑完 10 条 pilot 后决定是否升 MAIN——升了就把 `CPR_critical` 分母加 1。
4. **判定门槛与级联**：先 validity → 再 RULE/CVD/NLP 层 → 最后 VLM 层。每层都要报覆盖率。

---

## 6. Phase 2 开工清单（按此实现检查器）

**第一批（纯确定性，不依赖任何模型调用——优先做）**

| 项               | 检查方式 | 备注                                                                                  |
| --------------- | ---- | ----------------------------------------------------------------------------------- |
| P1 对比度          | RULE | WCAG 相对亮度公式自己写，约 20 行                                                               |
| P3 字号           | RULE | 解析 SVG `<text font-size>`                                                           |
| P9/U7/U9 轴与标题   | RULE | 文本节点 + 变量名比对                                                                        |
| U1/A3/C1 三件套存在性 | RULE | 依赖输出契约扩展（§3a）                                                                       |
| P7 色觉安全         | CVD  | `colorspacious` 或自写 Machado 矩阵 + ΔE2000                                             |
| U3 阅读等级         | NLP  | `textstat`                                                                          |
| P6 重叠           | GEO  | **复用 viseval 自带的 `overlapScript` / `overflowScript`**（Chrome CLI `--dump-dom`），不要重写 |

**第二批（VLM，放最后，且只服务 WATCH 项）**：P5、P8、A5 —— 复用 Chrome 截图 + `qwen3-vl:8b`。

**依赖**：`pip install colorspacious textstat`（进 viseval conda 环境）；ΔE2000 若不想装 scikit-image 可自写。

**质量门槛**：

- 检查器自身必须有 pytest：**每条至少 2 个的合成 SVG fixture**（一个必挂、一个必过），延续现有 41 个测试的作风
- 任何批量运行前先跑 **10 个 instance 的 pilot**，人工核对规则层判错率并实测耗时（历史教训：曾按错误估算把 3–5 小时的活当成 10 小时的活）

---

## 7. 目标二预备（暂不实现）

Lundgard & Satyanarayan（InfoVis'21/TVCG）四层语义模型 —— 映射时顺带对齐：

| 层       | 内容            | 与本工程的关系                                                       |
| ------- | ------------- | ------------------------------------------------------------- |
| L1 构造属性 | 图型 / 轴 / 编码   | 相当于 A3 之外的"基本描述"                                              |
| L2 统计关系 | 极值 / 趋势 / 比较  | **盲人读者最看重**（论文实测 30 盲人 + 90 明眼，两组差异显著）；数值可用数据表程序化校验 → 幻觉检测切入点 |
| L3 感知认知 | 复杂模式 / 异常     | 需模型"看出" ICT patterns                                          |
| L4 领域洞见 | 因果 / 上下文 / 建议 | 需要外部知识                                                        |

→ 上面 A3 那条 MAIN 项就是 target：**用同一套 agent 同时产出合规图 + L1–L3 描述**，目标二不需要另起炉灶。

> 另需修正：PLAN.md §4-T10 里写的"L1 元素 / L2 编码 / L3 关系 / L4 上下文"与论文原意有出入，以上表为准。

---

## 8. 引用与许可

- Chartability © Frank Elavsky，**CC-BY-SA 3.0**，[workbook](https://chartability.github.io/POUR-CAF/)；改编本表需同样署名并采用相同许可。


- Elavsky, F., Bennett, C., Moritz, D. *How accessible is my visualization? Evaluating visualization accessibility with Chartability.* EuroVis 2022.
- Lundgard, A., Satyanarayan, A. *Accessible Visualization via Natural Language Descriptions: A Four-Level Model of Semantic Content.* IEEE TVCG (Proc. InfoVis), 2022. arXiv:2110.04406.
