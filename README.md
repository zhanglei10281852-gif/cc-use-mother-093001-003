# 雨污溢流证据与整改闭环

面向街区管理方的溢流事件管理服务：把排水公司、环卫、施工单位各自保存的
传感读数、人工巡查、实验室样本与作业回执，按**可信时间线**统一归档，
由证据推导影响区段、处置阶段与整改项，并跟踪整改依赖、复验结果与逾期
升级，支撑复盘时"污染何时出现、哪项处置生效、材料是原始还是后补"的举证。

## 核心规则

- **只追加不覆盖**：所有事实写入哈希链事件日志（`events.jsonl`），撤回、
  确认都是新事件，原始记录永不修改、永不删除；`verify-chain` 可校验完整性。
- **补录管控**：登记时间晚于观测时间超过阈值（默认 24 小时）或显式标记的
  证据视为补录，必须填写理由，并经**两名不同确认人**（均非登记人）确认后
  才参与结论；确认前为 `pending`，不计入任何结论。
- **结论可重算**：影响区段、处置阶段、整改项由"已确认且未撤回"的证据纯函数
  推导；撤回一份证据后结论自动重算，已签发的旧报告原样保留。
- **批量导入隔离**：坏记录（缺字段、类型非法、编号重复、补录缺理由等）进入
  独立隔离区（`quarantine.jsonl`）并附原因，绝不污染已确认数据；
  `atomic` 模式下任一坏记录即整批拒绝。
- **报告可校验**：签发生成覆盖全部输入证据哈希与结论的 SHA-256 摘要，并与
  上一版报告哈希衔接成链；`verify-report` 重算摘要并核对证据链。

## 推导规则（确定性）

- **影响区段**：按位置聚合污染性证据（传感超限、样本超限、巡查见溢流），
  以超限倍数计分定级（minor/moderate/severe）；其后出现恢复性证据则记为已清除。
- **处置阶段**：污染发现 → 围挡封控 → 清掏处置 → 复验确认，各阶段起止时间
  取自触发证据的观测时间。
- **整改项**：每个区段生成清掏（3 天）、检修（7 天，仅 severe）、复验
  （10/14 天）三类整改项，含依赖关系与完成条件；逾期按 1 提醒 / 2 督办 /
  3 挂牌 三级升级。

## 运行

```bash
python3 -m unittest discover -s tests -v     # 测试
python3 -m compileall -q src tests run_cli.py  # 编译检查
python3 run_cli.py                            # 冒烟（兼容原入口）
```

## 命令行

```bash
# 登记事件与证据（补录自动识别，需 --backfill-reason）
python3 run_cli.py --data-dir data init-incident --incident-id INC-1 --title 中秋冒溢
python3 run_cli.py --data-dir data add-evidence --incident-id INC-1 --kind sensor \
  --observed-at 2026-09-29T08:00:00+08:00 --location BLOCK-2 --source 排水公司 \
  --by 平台 --payload '{"parameter":"cod","value":45.0,"threshold":30.0}'

# 补录双人确认 / 撤回（结论自动重算，旧报告保留）
python3 run_cli.py --data-dir data confirm --evidence-id E-XXX --by 确认人乙
python3 run_cli.py --data-dir data withdraw --evidence-id E-XXX --reason 仪表故障 --by 复盘组

# 批量导入（partial 隔离坏记录；atomic 整批拒绝）
python3 run_cli.py --data-dir data import --incident-id INC-1 --file batch.json --mode partial

# 结论、报告与溯源
python3 run_cli.py --data-dir data conclusions --incident-id INC-1
python3 run_cli.py --data-dir data issue-report --incident-id INC-1 --by 复盘组
python3 run_cli.py --data-dir data verify-report --report-id RPT-INC-1-001
python3 run_cli.py --data-dir data diff-reports --incident-id INC-1 --a RPT-INC-1-001 --b RPT-INC-1-002
python3 run_cli.py --data-dir data provenance --report-id RPT-INC-1-001 --ref zone:ZONE-BLOCK-2

# 整改闭环：完成条件 / 复验 / 逾期升级
python3 run_cli.py --data-dir data remediation --incident-id INC-1
python3 run_cli.py --data-dir data reinspect --incident-id INC-1 \
  --item REM-INC-1-BLOCK-2-VERIFY --result pass --by 复盘组 --evidence-id E-XXX
python3 run_cli.py --data-dir data escalations --incident-id INC-1
python3 run_cli.py --data-dir data verify-chain
```

## HTTP 接口

```bash
python3 run_cli.py --data-dir data serve --port 8080
```

`POST /incidents`、`POST /incidents/{id}/evidence`、`POST /evidence/{id}/confirm`、
`POST /evidence/{id}/withdraw`、`POST /incidents/{id}/import`、
`POST /incidents/{id}/reports`、`GET /incidents/{id}/timeline|conclusions|remediation|escalations`、
`GET /incidents/{id}/reports/diff?a=&b=`、`GET /reports/{id}/verify`、
`GET /reports/{id}/provenance?ref=`、`GET /chain/verify`。
错误映射：参数非法 400、对象缺失 404、状态冲突 409。

## 数据布局

```
data/
  events.jsonl       # 哈希链事件日志（唯一事实来源，只追加）
  quarantine.jsonl   # 导入隔离区（坏记录+原因，与主日志隔离）
  reports/RPT-*.json # 已签发报告（不可变快照，含可校验摘要）
```
