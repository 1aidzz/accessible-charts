# accessible-charts

给 matplotlib 生成的图表自动添加 **四层 alt-text**（无障碍描述），并用**规则 + LLM 双重质检**。

## 这是什么

视障用户使用读屏软件访问网页图表时，需要一段机器可读的描述文字（alt-text）。传统做法是人工写，成本高且容易遗漏数据细节。本项目自动完成这件事：

1. **生成 alt-text**：用 LLM 按 Lundgard & Satyanarayan (2021) 的四层结构生成描述
2. **质检 alt-text**：先跑 13 条规则检查（CPR 分数），再用 VLM 视觉模型和 LLM 文本模型打分

## 四层描述结构

| 层级 | 内容 | 示例 |
|---|---|---|
| **L1 construction** | 图表构造：类型、坐标轴、单位 | "Bar chart of January temperature, four times of day" |
| **L2 statistics** | 统计数值：极值、具体数字 | "12:00 is warmest at 9.7°C, 08:00 is coolest at 6.07°C" |
| **L3 patterns** | 感知模式：趋势、聚类、离群 | "Warm readings cluster at midday, morning and midnight stay cooler" |
| **L4 context** | 领域语境：这说明了什么 | "This suggests January days are warmest around midday" |

## 质检体系

- **规则层（CPR）**：13 条确定性检查（R0/P1-P7/U1-U9/C1-C2/A3-A4），检查 alt-text 是否完整、数值是否与图上数据一致、有没有冗余等
- **LLM 层（cpr_llm）**：视觉模型（Qwen）看图判断图表是否有数据标记，文本模型（GLM）对 L1-L4 各层打 0/1/2 分

## 目录结构

```
src/
├── a11y/
│   ├── checker.py       # 13 条规则检查，输出 CPR 分数
│   ├── alttext.py      # L1-L4 四层 alt-text 生成 + 规则核验
│   ├── vlm_layer.py    # VLM 视觉门 + LLM 文本评分（带磁盘缓存）
│   ├── contract.py      # ===A11Y=== 契约解析 + SVG <desc> 注入
│   ├── extract.py       # 从 matplotlib figure 反解 CSV 数据
│   ├── postprocess.py   # 图表样式后处理
│   └── llm_judge.py     # LLM 裁判原型
├── agents/
│   ├── image_to_chart.py    # 从图片描述生成 matplotlib 图表
│   └── local_qwen_agent.py   # 本地 Qwen 代码生成 agent
└── run_goal2.py         # 端到端入口
```

## 快速开始

### 环境要求

- Python 3.10+
- conda 环境（推荐 `viseval`）
- 环境变量 `OPENAI_API_KEY`（用于调用 LLM）

### 安装依赖

```bash
### 环境变量（可选）

LLM 相关配置都可以通过环境变量覆盖，不设则用默认值：

```bash
# 必填：LLM API Key
export OPENAI_API_KEY="your-key"

# 可选：LLM 服务地址（默认连校内服务器）
export CHART_LLM_BASE_URL="https://your-openai-compatible-endpoint"

# 可选：模型名（默认用 Qwen 视觉 + GLM 文本）
export CHART_VLM_MODEL="Qwen3.8-Flash"
export CHART_LLM_MODEL="GLM-5.3-Flash"

# 可选：浏览器路径（SVG 截图用，自动检测 Edge/Chrome）
export CHART_EDGE_PATH="/path/to/chrome"
```

不设 `OPENAI_API_KEY` 时，规则检查照常跑，LLM 层自动跳过。

### 运行

```bash
cd src
python run_goal2.py --image ../data/chartqa/multi_col_80264.png
```

### 输出

运行后会生成：
- `chart.svg` — 带 `<title>` 和 `<desc>` 的无障碍 SVG
- `alt_text.json` — 四层描述 + 质检结果
- `report.json` — CPR 分数和失败项详情

## 核心概念

- **CPR（Chart Pass Rate）**：规则检查通过率，0~1，critical 项必须全过
- **cpr_llm**：LLM 评分通过率，作为 advisory 参考，不进 critical
- **figure_csv**：从 matplotlib figure 反解的真实数据，用于和 alt-text 中的数字对账
- **磁盘缓存**：LLM 调用结果缓存在 `logs/.vlm_cache.json`，重复运行不重复花钱

## 参考文献

- Lundgard, A. & Satyanarayan, A. (2021). *Generating Plans from Sketches to Support Authoring of Data-Driven Accessible Charts.* CHI 2021.
- 四层 alt-text 体系基于 Lundgard & Satyanarayan 的 L1-L4 分类法。
