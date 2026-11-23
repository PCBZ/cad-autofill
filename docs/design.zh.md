# Candata CAD 自动录入系统设计

[English](design.md) | 中文

Nov 21, 2026 · Wenshuang（Nov 22 更新：AI 部分改用 CrewAI 编排）

> 以英文版 [design.md](design.md) 为准；修改设计时先改英文版，再同步本文。

## 背景与目标

系统把 B/L、商业发票（CI）和 EMF 三份清关资料交给 AI 识别，由程序模拟鼠标键盘填入 Candata Global Windows 客户端，产品明细通过 Excel 导入，最后由报关员复核并提交 CAD。AI 部分用 CrewAI 编排：Flow 管固定流程，Crew 只负责抽取和校验失败后的排查。

采用这条路线的原因：

- Candata 没有对我们开放 API，只能通过界面操作写入。
- Candata 支持发票明细的 Excel/CSV 导入，产品行不必逐行模拟录入。
- Candata 自带的 AI 发票识别效果不理想，且只覆盖商业发票，不处理 B/L 和 EMF。

范围之外：程序不自动提交 CAD，不修正 HS 编码，不建产品库，不使用 Candata 的 Shipment Workbook Import。

## 整体流程

一票货从拿到资料到提交前，经过五个步骤，只有第 5 步需要人工。

1. **AI 分析识别**：三份 PDF 先用 pymupdf 读出文字层（扫描件转人工或走本地 OCR 备选），再交给抽取 Crew：B/L、CI、EMF 三个专员各读一份单据，核对员汇总成一份结构化 JSON，分表头和产品明细两部分。程序做交叉校验，不通过时由排查 Crew 带工具复查；校验通过后，再由 Jev 核对单据类型、HS 编码和计量单位。
2. **生成产品 Excel**：明细部分按 Candata 发票明细导入模板生成 xlsx，HS 编码照发票原样。
3. **模拟操作填表**：程序打开 Candata 的 shipment 界面，用真实的鼠标点击和键盘输入逐个填写表头字段。
4. **导入产品**：程序点击导入按钮，在文件对话框里填入第 2 步生成的 Excel 路径，完成导入。
5. **人工复核**：程序停在提交前并截图存档，报关员检查、修改 HS 编码等字段，确认后自己提交。

抽取结果校验失败时，先交给排查 Crew；排查后仍不通过，或者其他任何一步出错，程序都会停下来提示，不会继续往下执行。

## 模块设计

系统分七个模块，前四个与 Candata 无关，可以在任何电脑上开发测试；后三个必须在装有 Candata 的 Windows 电脑上运行。前四个由 CrewAI Flow 串起来（见下面"CrewAI 编排"），后三个是普通代码，不使用 CrewAI。

| 模块 | 输入 | 输出 | 关键点 |
| --- | --- | --- | --- |
| extraction crew 抽取 | 三份 PDF | shipment JSON | pymupdf 读 PDF 文字层，用 find_tables() 保留发票表格行列；B/L、CI、EMF 三个专员 agent 并行抽取，核对员 agent 汇总，默认用 Claude Haiku 5.5 或 GPT-6 Luna；每个任务用 output_pydantic 约束输出、guardrail 检查格式；每个字段记录来源单据 |
| validator 校验 | shipment JSON | 校验报告 | 纯代码。明细金额合计 = 发票总额；提单号、件数、毛重、收货人在三份单据间一致 |
| investigation crew 排查 | shipment JSON + 校验报告 + 单据文字 | 修正后的 JSON + 排查记录 | 只在校验失败时运行，用 Claude Sonnet 5.5；带重读页面、重读表格、重算合计三个只读工具；每处修正写明依据，复核时标出 |
| excel_writer 生成 Excel | 明细部分 | 导入用 xlsx | 列顺序和格式按 Candata 模板，由 YAML 配置 |
| rpa_filler 填表 | 表头部分 | 已填好的 shipment 界面 | pywinauto 定位控件，`click_input()` + `type_keys()` 模拟真人；每个字段填完回读核对 |
| rpa_importer 导入 | xlsx 路径 | 已导入的产品行 | 点击导入按钮，在文件对话框填路径；导入后核对行数 |
| reviewer 复核留痕 | 全部结果 | 截图 + 日志 | 停在提交前，由报关员确认 |

填表方式按可靠性从高到低有三层，程序优先用第一层：

1. 按控件名定位（pywinauto UIA 或 win32 backend），再用真实鼠标点击和键盘输入。
2. 控件识别不到时，从已知字段出发按 Tab 顺序导航。
3. 最后才用屏幕坐标或图像识别（pyautogui），换分辨率或挪窗口就会失效。

下拉框和自动补全框（国家代码、口岸代码、币种）单独处理：输入后等待候选列表出现，选中精确匹配项，而不是直接回车。

### CrewAI 编排

流程顺序是固定的，所以用 Flow 串起来；只有抽取和排查两处需要模型判断，才用 Crew。填表和导入（RPA）不进 Flow，也不做成任何 agent 的工具：不能让模型决定点不点鼠标、点几次。

```
load_texts → extract（抽取 Crew）→ validate ─ 通过 ─→ classify（Jev）→ write_excel → 结束
                                             └ 不通过 → investigate（排查 Crew）→ revalidate ─ 通过 ─→ classify …
                                                                                              └ 不通过 → 停止，转人工
```

| Flow 步骤 | 写法 | 说明 |
| --- | --- | --- |
| load_texts | `@start()`，代码 | pymupdf 读三份 PDF；文字过少判为扫描件，停止转人工 |
| extract | `@listen`，抽取 Crew | 输出 shipment JSON |
| validate | `@router`，代码 | 返回 `passed` 或 `failed` |
| investigate | `@listen("failed")`，排查 Crew | 输出修正和排查记录 |
| revalidate | `@router`，代码 | 通过进入 classify；不通过停止 |
| classify | `@listen(or_(...))`，代码 | 调 Jev，不经过 agent |
| write_excel | `@listen`，代码 | 写 xlsx、校验报告、排查记录 |

- Flow 的状态是一个 pydantic 模型（`ShipmentFlow(Flow[ShipmentState])`），装 shipment JSON、校验报告和排查记录；每票结束写到 `output/<票号>/`。
- 不用 `@persist`：两台电脑之间靠 JSON 文件交接；一票抽取只需几十秒，失败了整段重跑即可。

**抽取 Crew**：按单据分角色，和报关行的实际分工一致。

| Agent | 输入 | 输出 |
| --- | --- | --- |
| B/L 专员 | B/L 文字 | 提单号、收发货人、装卸港、件数与包装、毛重、船名航次、柜号 |
| CI 专员 | CI 文字和表格 | 发票头（发票号、日期、买卖方、币种、成交条款、总额）和产品明细 |
| EMF 专员 | EMF 文字 | CCN、承运人代码、入境口岸、ETA，以及供核对的提单号、件数、毛重 |
| 核对员 | 三个专员的输出 | 合并成一份 shipment JSON；统一同一实体的不同写法（如收货人全称和简称）；列出冲突字段 |

- 三个专员的任务设 `async_execution=True` 并行，核对员的任务用 `context` 接收它们的输出；process 用 sequential，不需要经理 agent 分派（hierarchical）。
- 每个任务用 `output_pydantic` 指定 `schema.py` 里的模型；`guardrail` 函数检查必填字段和格式（日期、数字、国家代码），不合格打回重做，重试次数用 `guardrail_max_retries`（默认 3）。
- 核对员只负责合并和统一写法；金额合计、三单一致这些硬规则由 validator 用代码判断，不交给模型。

**排查 Crew**：一个排查员 agent，用 Claude Sonnet 5.5，只在校验失败时运行。

| 工具 | 作用 |
| --- | --- |
| `read_page_text(doc, page)` | 重读某份单据某一页的原文 |
| `read_page_tables(doc, page)` | 用 find_tables() 重读某页表格 |
| `recompute_totals(lines)` | 重算明细合计，与发票总额比对 |

- 输出 `InvestigationResult`：每个校验问题一条，结论是 `corrected`（抽错了，给出修正值）、`document_mismatch`（单据本身不一致）或 `unresolved`，并附依据（单据、页码、原文片段）。
- 修正由代码写回 JSON 后重新校验；排查修正过的字段在校验报告和复核截图里标出，由报关员确认。
- 三个工具都是只读的，排查员不能调用填表、导入或任何写操作。

**配置**

- agent 的 role、goal、backstory 和任务描述放在各 crew 的 `config/agents.yaml`、`config/tasks.yaml`（`crewai create crew <name> --classic` 生成的结构）；每个 agent 用哪个模型写在 `settings.yaml`。
- 模型用 CrewAI 的 `LLM` 类配置：Anthropic、OpenAI 有原生集成（`crewai[anthropic]`、`crewai[openai]`）；OpenRouter 等其他提供方走 LiteLLM（`crewai[litellm]`，模型名写成 `openrouter/<vendor>/<model>`）。
- 关闭遥测和 tracing：`.env` 里设 `CREWAI_DISABLE_TELEMETRY=true`、`OTEL_SDK_DISABLED=true`、`CREWAI_TRACING_ENABLED=false`。tracing 记录可能包含提示词和单据内容，不能上传。

来源：[CrewAI Flows](https://docs.crewai.com/en/concepts/flows)、[Tasks](https://docs.crewai.com/en/concepts/tasks)、[LLMs](https://docs.crewai.com/en/concepts/llms)、[Telemetry](https://docs.crewai.com/en/telemetry)、[Tracing](https://docs.crewai.com/en/observability/tracing)、[安装](https://docs.crewai.com/en/installation)

### Jev 分类

PDF 经 pymupdf 提取文字、由抽取 Crew 整理成 JSON 后，分类判断交给 TypeSafe AI 的 Jev：Jev 只接受文本，对预先声明的问题返回带校准置信度的结构化答案，适合在代码里做判断。它在 Flow 的 classify 步骤里，校验通过之后、生成 Excel 之前，由代码直接调用，不经过 agent。

| 任务 | Jev 问题 | 输入 | 结果怎么用 |
| --- | --- | --- | --- |
| 单据类型识别 | Choice：B/L、CI、EMF、其他 | 每份 PDF 首页文字 | 自动归类，缺件或放错时提示 |
| HS 编码核对 | Choice：匹配、不匹配、无法判断 | 货物描述 + CI 上的 HS 编码及其税则描述 | 不匹配的行在校验报告标红，交给人工 |
| HS 编码建议 | 分层 Choice：章 → 品目 → 子目 → 10 位税号 | 货物描述 + 每层候选列表 | 作为建议列写进报告，导入仍用 CI 原值，人工决定是否采纳 |
| 计量单位映射 | Choice | CI 上的单位 + Candata 单位代码表 | 写入导入 Excel |

- 一个 Choice 最多 255 个选项，所以 HS 建议必须逐层选；每层候选来自加拿大海关税则（Customs Tariff），需要整理成本地数据文件放在 `config/tariff/`。
- 每个答案带置信度，低于阈值就留空并标红；阈值写在 settings.yaml，用样本资料调。
- Jev 目前是封闭的托管 API，官方通道需要申请 early access；不排队的方式是 Vercel AI Gateway（typesafe-ai/jev）或 Cloudflare Workers AI（typesafe/jev）。分类模块同样走统一接口，Jev 不可用时可以换成 Gemini 或 Azure 模型。

来源：[awesome-jev 资料汇总](https://github.com/kraayenjon/awesome-jev/wiki)、[TrueFoundry 对 Jev 的介绍](https://www.truefoundry.com/de/blog/typesafe-ai-jev)

## 数据结构

所有模块共用一份 shipment JSON，它也是 Flow 状态的主体：`header` 给填表模块，`lines` 给 Excel 模块，每个字段带 `source` 标明来自哪份单据，`investigation` 记录排查 Crew 改过什么、依据是什么。字段清单是初稿，待对照 Candata 界面和导入模板定稿。

| 字段 | 来源 | 用途 |
| --- | --- | --- |
| bl_number 提单号 | B/L（EMF、CI 交叉核对） | 表头 |
| cargo_control_number CCN | EMF | 表头 |
| carrier_code 承运人代码 | EMF | 表头 |
| port_of_entry 入境口岸 | EMF | 表头 |
| eta 预计到港 | EMF | 表头 |
| shipper / consignee 收发货人 | B/L（CI 核对） | 表头 |
| port_of_loading / discharge 装卸港 | B/L | 表头 |
| packages / package_type 件数与包装 | B/L（EMF 核对） | 表头 |
| gross_weight 毛重 | B/L（EMF 核对） | 表头 |
| vessel_voyage / container 船名航次与柜号 | B/L | 表头 |
| invoice_number / date 发票号与日期 | CI | 表头 |
| vendor / purchaser 卖方与买方 | CI | 表头 |
| currency / incoterms 币种与成交条款 | CI | 表头 |
| invoice_total 发票总额 | CI | 校验 |
| description 货物描述 | CI | 明细 |
| hs_code HS 编码 | CI（原样） | 明细 |
| country_of_origin 原产国 | CI | 明细 |
| quantity / unit 数量与单位 | CI | 明细 |
| unit_price / amount 单价与金额 | CI | 明细 |

示例（省略部分字段）：

```json
{
  "header": {
    "bl_number": {"value": "MAEU123456789", "source": "bl"},
    "cargo_control_number": {"value": "", "source": "emf"},
    "currency": {"value": "USD", "source": "ci"}
  },
  "lines": [
    {"line_no": 1, "description": "", "hs_code": "", "country_of_origin": "CN",
     "quantity": 100, "unit": "PCS", "unit_price": 2.5, "amount": 250.0}
  ],
  "validation": {"passed": true, "issues": []},
  "investigation": [
    {"field": "header.gross_weight", "result": "corrected", "old": 12400, "new": 1240,
     "evidence": {"doc": "bl", "page": 1, "quote": "GROSS WEIGHT 1,240.00 KGS"}}
  ]
}
```

## 依赖

运行环境是 Windows 10/11 + Python 3.10–3.13（64 位，建议 3.12；CrewAI 当前要求 >=3.10,<3.14），外加所选 LLM 提供方的凭据；下表版本号为最低要求，联调通过后应锁定具体版本。

### Python 包

| 包 | 最低版本 | 用途 | 模块 |
| --- | --- | --- | --- |
| pymupdf | 1.24 | 读取 PDF 文字层，find_tables() 提取表格，检测扫描件 | text |
| crewai | 1.15 | Flow 编排、抽取与排查 Crew、调用 LLM；按提供方装 extra | flow、crews |
| typesafe-sdk | 待联调确定 | 调用 Jev 做分类 | classifier |
| pydantic | 2.5 | 定义并校验 shipment JSON 结构、Flow 状态、任务输出 | schema、flow、validator |
| openpyxl | 3.1 | 生成导入用 xlsx | excel_writer |
| pywinauto | 0.6.8 | 识别 Candata 控件，模拟鼠标键盘 | rpa_filler、rpa_importer |
| pyautogui | 0.9.54 | 坐标点击后备方案、屏幕截图 | rpa_filler、reviewer |
| Pillow | 10.0 | 保存和处理截图 | reviewer |
| keyboard | 0.13.5 | 全局紧急停止热键（Esc） | rpa_filler |
| PyYAML | 6.0 | 读取字段映射和界面定位配置 | 全部 |
| python-dotenv | 1.0 | 从 .env 读取凭据和 CrewAI 环境变量 | flow、classifier |

crewai 按选定的模型提供方装一个 extra：Anthropic 用 `crewai[anthropic]`，OpenAI 用 `crewai[openai]`，OpenRouter 等其他提供方用 `crewai[litellm]`。每个 agent 的模型写在 settings.yaml，换模型只改配置；classifier 仍定义统一接口，Jev 不可用时可以换成 Gemini 或 Azure 模型。扫描件读不出文字时默认转人工；如果扫描件多，再加入本地免费的 PaddleOCR（`paddleocr` + `paddlepaddle`）作备选。

依赖分成两个文件：抽取那一段可以在任何电脑上跑，Candata 电脑只做填表时不必装 CrewAI。

`requirements-extract.txt`（`main.py extract`）：

```
# 文字提取
pymupdf>=1.24
# paddleocr + paddlepaddle   # 扫描件本地 OCR 备选

# 编排与抽取：按选定的模型提供方三选一，联调后锁定版本
crewai[anthropic]>=1.15      # Claude Haiku 5.5 / Sonnet 5.5
# crewai[openai]>=1.15       # GPT-6 Luna
# crewai[litellm]>=1.15      # OpenRouter 等其他提供方

# 分类
typesafe-sdk                 # Jev，联调后锁定版本

pydantic>=2.5
openpyxl>=3.1
PyYAML>=6.0
python-dotenv>=1.0
```

`requirements-fill.txt`（`main.py fill`，Candata 电脑）：

```
pydantic>=2.5
pywinauto>=0.6.8
pyautogui>=0.9.54
Pillow>=10.0
keyboard>=0.13.5
PyYAML>=6.0
```

安装（两段都在同一台电脑上跑时两个都装）：

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-extract.txt
.venv\Scripts\python.exe -m pip install -r requirements-fill.txt
```

### 系统与工具

- **Candata Global Windows 客户端**：已登录，脚本与客户端要在同一权限级别运行；客户端若以管理员身份启动，脚本也必须以管理员身份运行，否则 pywinauto 读不到控件。
- **显示设置**：缩放固定为 100%，Candata 窗口最大化，运行期间不锁屏、不远程断开。
- **Inspect.exe**（Windows SDK 自带）或 Accessibility Insights：开发时查看控件名和层级。
- **Microsoft Excel**：非必需，仅用于人工查看生成的 xlsx。

### 外部服务

- **Anthropic API**（Claude Haiku 5.5 / Sonnet 5.5）或 **OpenAI API**（GPT-6 Luna）：二选一，需要对应的 API key。也可以用 **OpenRouter**：一个 key 调多家模型，token 单价不加价，充值收 5.5% 手续费（每次至少 $0.80）。
- **CrewAI**：开源库，在本地运行，不需要 CrewAI 账号；遥测和 tracing 按"CrewAI 编排"一节关闭。
- **Jev（TypeSafe AI）**：需要 TYPESAFE_API_KEY；官方 API 需申请 early access，也可以通过 Vercel AI Gateway 或 Cloudflare Workers AI 调用。
- 文字提取在本地完成，不依赖外部 OCR 服务。
- 以上服务按用量计费，模型名写在配置里便于更换；单据文字会发送到外部服务，使用前需确认符合公司和客户的数据处理要求。

注册步骤和 `.env` 变量名见 README 的"Accounts and API keys"一节。

## 项目结构与配置

字段映射、界面定位和 Excel 列顺序都放在 `config/` 的 YAML 里，Candata 界面或模板变了只改配置，不改代码。agent 和任务的提示词放在各 crew 自己的 `config/` 里。

```
cad-autofill/
├── config/
│   ├── settings.yaml        # 每个 agent 的模型、guardrail 重试次数、置信度阈值、目录、超时
│   ├── header_fields.yaml   # JSON 字段 → Candata 控件定位 → 格式规则
│   ├── excel_template.yaml  # Candata 发票导入模板的列顺序与格式
│   └── tariff/              # 加拿大海关税则分层候选、Candata 单位代码表
├── src/
│   ├── text/
│   │   ├── pdf_text.py      # pymupdf 文字层与表格提取、扫描件检测
│   │   └── paddle.py        # 本地 OCR 备选
│   ├── flow.py              # ShipmentFlow：load_texts → extract → validate → … → write_excel
│   ├── crews/
│   │   ├── extraction/
│   │   │   ├── config/agents.yaml   # B/L、CI、EMF 专员与核对员
│   │   │   ├── config/tasks.yaml
│   │   │   ├── guardrails.py        # 必填字段与格式检查
│   │   │   └── crew.py
│   │   └── investigation/
│   │       ├── config/agents.yaml   # 排查员
│   │       ├── config/tasks.yaml
│   │       └── crew.py
│   ├── agent_tools/         # 排查用只读工具：read_page_text、read_page_tables、recompute_totals
│   ├── classifier/
│   │   ├── base.py          # 分类接口：单据类型、HS 核对与建议、单位映射
│   │   └── jev.py
│   ├── schema.py            # pydantic 模型：shipment、Flow 状态、InvestigationResult
│   ├── validator.py
│   ├── excel_writer.py
│   ├── rpa_filler.py
│   ├── rpa_importer.py
│   └── reviewer.py
├── tools/
│   └── scan_candata_controls.py
├── inbox/                   # 每票货一个子文件夹，放三份 PDF
├── output/                  # 提取文本、JSON、xlsx、校验报告、截图、日志
├── main.py
├── requirements-extract.txt
├── requirements-fill.txt
└── .env                     # 模型 key（ANTHROPIC_/OPENAI_/OPENROUTER_API_KEY 之一）、TYPESAFE_API_KEY、
                             # CREWAI_DISABLE_TELEMETRY、OTEL_SDK_DISABLED、CREWAI_TRACING_ENABLED；不进版本库
```

`header_fields.yaml` 示例：

```yaml
bl_number:
  locator: {auto_id: "txtBillOfLading"}   # 待控件扫描后确定
  fallback: {tab_from: "txtShipmentNo", tabs: 3}
  type: text
port_of_entry:
  locator: {title: "Port", control_type: "ComboBox"}
  type: combobox                           # 输入后等待候选并精确匹配
  format: "{value:0>4}"
```

运行方式分两档：`python main.py extract inbox/<票号>` 运行 ShipmentFlow，只做识别和生成 Excel，可在任何电脑运行；`python main.py fill inbox/<票号>` 在 Candata 电脑上执行填表和导入，读取 extract 生成的 JSON 和 xlsx，不使用 CrewAI。

## 风险与注意事项

最大的风险是 Candata 界面控件识别不到，这决定了 RPA 的稳定性，应该最先验证。

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 控件识别不到（自绘界面） | 只能用 Tab 导航或坐标，容易填错 | 先跑 scan_candata_controls.py 评估；每个字段填完回读核对 |
| Candata 升级改界面 | 定位失效 | 定位全部放配置；启动时自检关键控件是否存在 |
| AI 识别错误 | 表头或明细数据错 | 交叉校验；低置信度字段标红；人工复核后才提交 |
| 多 agent 输出不稳定、token 用量变多 | 同一票多跑几次结果可能不同；成本高于单次调用 | output_pydantic + guardrail 约束格式；硬规则用代码校验；用样本实测 token 用量和通过率后再定模型 |
| 排查 Crew 改错数据 | 把正确的值"修正"成错的 | 每处修正必须附原文依据；修正过的字段在复核时标出；排查员只有只读工具 |
| CrewAI 版本更新快 | 升级后接口变化 | 锁定版本；升级前用样本回归 |
| CrewAI 遥测或 tracing 上传 | 单据内容外传 | 用环境变量关闭遥测和 tracing；上线前确认没有对外上报 |
| CI 上的 HS 编码是出口国 6/8 位编码 | 导入可能报错，或与加拿大 10 位税则号不符 | 原样导入，人工修正；若导入拒绝则留空 |
| 运行中有人动鼠标键盘 | 填到错误位置 | Esc 紧急停止；运行前弹窗提示；焦点丢失即中止 |
| 弹窗或加载慢 | 脚本卡住或提前操作 | 每一步显式等待控件出现，不用固定 sleep；未知弹窗截图并停止 |
| 客户单据发送到外部 API | 数据合规 | 上线前确认公司和客户的数据处理政策 |
| 自动化操作可能受软件许可限制 | 合同风险 | 向 Candata 确认服务协议是否允许 |

pymupdf 只能读出电子版 PDF 的文字层：扫描件或图片型 PDF 读不到文字，程序检测到文字过少时转人工；上线前统计扫描件占比，占比高再启用 PaddleOCR 备选。

CAD 由持牌报关员负责，程序始终停在提交前，提交动作只由人完成。

## 成本估算

用 pymupdf 读文字层、CrewAI 多 agent 抽取时，API 成本约每票 0.02 美元，每月 1,000 票约 20 美元，约为之前 Azure OCR + Gemini 方案（约 0.10 美元/票）的五分之一。单次调用抽取的写法约 0.015 美元/票，多出的部分是多 agent 的代价。

| 项目 | 单价（每百万 token，输入 / 输出） | 每票用量（假设） | 每票成本（USD） |
| --- | --- | --- | --- |
| 文字提取：pymupdf | 免费，本地运行 | 5 页 | 0 |
| 抽取 Crew：Claude Haiku 5.5 或 GPT-6 Luna | $0.10 / $0.50 | 4 次调用（3 个专员 + 核对员），输入 15,000–20,000、输出 8,000–10,000 token | 约 0.006–0.007 |
| 排查 Crew：Claude Sonnet 5.5（假设 10% 的票） | $2 / $10 | 每次输入约 30,000、输出约 3,000 token（多轮工具调用会重复发送上下文） | 约 0.009（摊到每票） |
| 分类：Jev | 输入 $0.042 | 约 100,000 token（20 行明细，含分层 HS 建议） | 约 0.004 |
| **合计** | | | **约 0.02** |

- 假设：每票 5 页（B/L 1–2 页、CI 1–3 页、EMF 1 页），每页文字约 1,500 token；Haiku 5.5 的分词器对同样文本多产生约 30% token，已计入区间上限。抽取的用量比单次调用多，是因为每个 agent 都带 CrewAI 的角色提示，核对员还要再读一遍三个专员的输出。
- Haiku 5.5 的低价只适用于 10 万 token 以内的 prompt，每次调用的 prompt 都在 1–2 万 token 以内，远低于门槛。
- 排查比例是最大的变量：不触发排查时合计约 0.01 美元/票；排查比例升到 30% 时约 0.04 美元/票。guardrail 打回重做也会增加用量，没计入上表。用样本资料实测 token 用量和校验通过率后再修正。
- 扫描件转人工不产生 API 费用；启用 PaddleOCR 备选也免费，只占本地 CPU。
- 不含：开发人力、运行 Candata 的 Windows 电脑、Candata 许可。CrewAI 是开源库，不收费。Jev 仍在 early access，定价可能变化。

来源：[Claude 定价](https://platform.claude.com/docs/en/about-claude/pricing)、[Haiku 5.5 分档定价与 Sonnet 5.5 价格](https://mixed-news.com/en/?p=35958)、[Haiku 5.5 分词器 token 变化](https://mixed-news.com/en/?p=35955)、[GPT-6 Luna 定价（Simon Willison）](https://simonwillison.net/tags/llm-pricing)、[TrueFoundry 对 Jev 的介绍](https://www.truefoundry.com/de/blog/typesafe-ai-jev)

## 待确认事项与下一步

动手写填表和 Excel 模块之前，需要先拿到以下信息。

- [ ] 在 Candata shipment 表头界面运行 scan_candata_controls.py，确认控件能否识别
- [x] 拿到 Candata 发票明细导入模板的列标题和格式要求（帮助中心文章：Shipment Invoice details loading via Excel/CSV spreadsheets）
- [x] 列出人工录入时从 B/L 和 EMF 抄到 Candata 的字段，及对应的界面字段名
- [x] 确认 EMF 的具体格式（ACI eManifest 导出的 PDF，还是其他）
- [x] 确认 Candata 导入时是否校验 HS 编码位数
- [x] 准备 3–5 票隐去敏感信息的样本资料，用于测试抽取准确率
- [x] 选定 LLM 提供方，准备凭据并完成数据合规审批
- [ ] 用 1–2 票样本跑通抽取 Crew，实测 token 用量和校验通过率
- [ ] 锁定 CrewAI 版本，确认遥测和 tracing 已关闭

开发顺序：

1. Flow 骨架 + 抽取 Crew + validator + excel_writer：用样本资料跑通识别到 xlsx，人工导入验证。
2. 排查 Crew：用有真实冲突或故意改错的样本测试，确认每处修正都有原文依据。
3. rpa_importer：自动完成 Excel 导入，这一步界面操作少，适合先验证 RPA 可行性。
4. rpa_filler：逐步加入表头字段，先做文本框，再做下拉框和自动补全。
5. reviewer + 完整流程串联，小批量试运行。
