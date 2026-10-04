# 雨污溢流证据与整改闭环

面向老城文旅街区的溢流事件管理服务：把传感读数、人工巡查、实验室样本和作业回执
按可信时间线归档，由证据推导影响区段、处置阶段与整改项，跟踪整改依赖、复验与
逾期升级，并签发可校验、可对比的事件报告。

## 核心规则

- **只追加不覆盖**：证据入账后不可修改；撤回只是追加撤回记录，原始条目保留，
  台账条目以哈希链串联（`verify-chain` 可校验篡改）。
- **补录双人确认**：早于事件建立时间、或入账时间距发生时间超过 24 小时的记录
  属于补录，必须填写理由并经**两名不同人员**确认，否则拒收。
- **结论可复核**：影响区段（任一超标证据所在的 location）、处置阶段
  （detection → containment → cleaning → restoration → verification）与整改项
  （封控/清掏/修复/复验，含依赖与期限）全部由有效证据推导，每项结论都能反查
  所依据的证据编号。
- **撤回重算、旧报告保留**：撤回证据后重新签发报告即得到重算后的新版本；
  已签发报告不可变，`diff` 可对比任意两版在证据、区段、整改上的差异。
- **批量导入隔离**：逐条校验，坏记录进隔离区并附错误原因，不污染已确认数据。
- **报告摘要可校验**：每份报告带 sha256 摘要，`verify` 重新计算比对。

## 证据类型与 payload

| kind | 必填字段 | 说明 |
| --- | --- | --- |
| `sensor` | `metric, value, limit` | `value > limit` 记为超标 |
| `inspection` | `overflow_observed` | 为 true 记为冒溢 |
| `sample` | `analyte, value, limit` | `value > limit` 记为超标 |
| `work_receipt` | `action, completed` | action ∈ sealing / pumping / cleaning / disinfection / repair / other |

所有时间必须是带时区的 ISO 格式。

## 命令行

```bash
CLI="python3 -m overflow_evidence.cli --db overflow_db.json"  # 需 PYTHONPATH=src

$CLI create-incident INC-1 --opened-at 2026-09-29T08:00:00+08:00 --description 中秋冒溢
$CLI submit INC-1 --kind sensor --location BLOCK-2 \
    --observed-at 2026-09-29T09:00:00+08:00 --payload '{"metric":"cod","value":120,"limit":40}'
$CLI submit INC-1 --kind sample --location BLOCK-2 \
    --observed-at 2026-09-29T05:00:00+08:00 --payload '{"analyte":"cod","value":90,"limit":40}' \
    --backfill-reason 实验室节后补送 --confirm 张三 --confirm 李四   # 补录
$CLI import INC-1 --file batch.json            # 批量导入，坏记录进隔离区
$CLI timeline INC-1                            # 可信时间线
$CLI conclusions INC-1                         # 当前结论（实时推导）
$CLI report INC-1                              # 签发报告（含摘要）
$CLI withdraw EV-00001 --reason 传感器漂移 --by 值班长
$CLI report INC-1                              # 重算后的 v2，v1 保留
$CLI diff RPT-INC-1-1 RPT-INC-1-2              # 历次报告差异
$CLI evidence-of RPT-INC-1-1 segment:BLOCK-2   # 结论所依据的材料
$CLI evidence-of RPT-INC-1-1 stage:BLOCK-2:containment
$CLI evidence-of RPT-INC-1-1 remediation:contain:BLOCK-2
$CLI completion INC-1:contain:BLOCK-2          # 整改完成条件
$CLI reinspect INC-1:contain:BLOCK-2 --evidence EV-00003 --passed yes --by 王工
$CLI escalate                                  # 逾期升级检查
$CLI verify RPT-INC-1-1                        # 校验报告摘要
$CLI verify-chain                              # 校验台账哈希链
$CLI serve --port 8080                         # 启动 HTTP 接口
```

## HTTP 接口

`serve` 启动后提供与命令行等价的能力，主要包括：

- `POST /incidents`、`POST /incidents/<id>/evidence`、`POST /incidents/<id>/import`
- `POST /evidence/<id>/withdraw`、`POST /incidents/<id>/reports`
- `GET /incidents/<id>/timeline`、`GET /incidents/<id>/conclusions`
- `GET /reports/<id>`、`GET /reports/<id>/verify`
- `GET /reports/<id>/evidence?ref=segment:BLOCK-2`（结论依据的材料）
- `GET /reports/diff?old=...&new=...`（历次报告差异）
- `GET /remediations/<task_id>/completion`（整改完成条件）
- `POST /remediations/<task_id>/reinspections`、`POST /escalations/check`

## 整改闭环

每个影响区段派生四项整改：`contain`（4h）→ `clean`（24h）→ `restore`（48h）
→ `verify`（72h），期限为首次超标后的相对小时数。证据满足完成条件后任务进入
`satisfied`（待复验）；登记复验通过（要求条件已满足且依赖项均已复验通过）后
进入 `verified`；复验不通过回退 `open`；超过期限未闭环的任务在 `escalate`
检查中升级为 `escalated` 并累积升级记录。撤回证据会使满足状态自动回退。

## 运行测试与检查

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests run_cli.py
python3 run_cli.py   # 端到端冒烟
```
