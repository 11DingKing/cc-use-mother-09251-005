# 新能源道路救援派单

假期道路救援热线同时处理**低电量趴窝、充电设备故障、普通事故**。本系统登记求援位置、
电池余量、乘员风险与救援队能力，综合**道路方向与可达性、补能机会、服务区充电拥堵、
值班窗口（时区）与优先级**生成派单；定位更新或道路封闭时只调整尚未承诺的部分，
跨机构转派全程保留责任交接凭据。

纯服务端实现，仅依赖 Python 3.11 标准库（`zoneinfo` 需系统时区数据）。

## 分层结构

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 领域 | `domain/network.py` | 有向道路网（双向/单向、封闭事件）、Dijkstra 可达性与里程 |
| 领域 | `domain/station.py` | 服务区快充站：占用率、排队等待、占位/释放、离线 |
| 领域 | `domain/team.py` | 救援队能力、驻点、值班窗口（IANA 时区、跨午夜、工作日） |
| 领域 | `domain/cases.py` | 工单、乘员风险、重复呼叫合并（风险只升不降）、SLA 与升级、隐私授权 |
| 领域 | `domain/assignment.py` | 派单、补能记录、跨机构交接凭据链 |
| 派单 | `dispatch/planner.py` | 可达性 × 补能方式 × 拥堵等待 × 值班覆盖 × 风险加权评分 |
| 应用 | `application/service.py` | 用例编排：受理/抢单/转派/到场/补能/关闭、只重规划未承诺部分 |
| 应用 | `application/compensation.py` | 充电位释放的补偿台账（Saga）：失败退避重试、重启继续、超限转人工 |
| 应用 | `application/repository.py` | 线程安全内存仓储 + JSON 原子快照（重启恢复全部运行态） |
| 接口 | `interfaces/api.py` | 纯函数 JSON 路由（可直接单测）+ `http.server` 适配 |
| 引导 | `__main__.py` | 种子装载、快照恢复、HTTP 服务与后台 tick |

时间、标识与外部资源均通过可替换端口接入：`clock.Clock`（`SystemClock`/`FixedClock`）、
`compensation.StationGateway`，因此状态变化可稳定复现。

## 核心规则

- **可达性联动**：规划按当前有向路网算最短路径；道路封闭后未承诺派单整体重算
  （可能改走绕行或改用移动补能），已抢单承诺的派单冻结不撤（队伍已在途）。
- **补能方式**：移动补能车与“拖至可达服务区”互为候选，综合到场时间与拖车里程评分；
  趴窝低电量场景移动补能有小幅优先。
- **充电拥堵**：服务区无空位时方案仍成立但把排队等待折算进评分；有空位才占位预约，
  满站车辆到场排队；服务区离线则剔除。派单撤销/过期时释放预约，释放失败进补偿台账。
- **值班窗口**：以救援队本地时区表达，支持跨午夜窗口与工作日集合；整个处置区间必须
  被同一窗口覆盖才可派单。正确处理 DST（如柏林夏令时切换）。
- **优先级**：由风险等级与升级档位共同决定；高风险对响应时间赋予更高权重。
- **重复呼叫**：同电话/车牌、同事件类型、时间窗内归并；风险标志只升不降、电池取最小
  值、SLA 只收紧，最高风险来源呼叫被显式标记，绝不被合并吞掉。
- **超时升级**：按墙钟与计划期限连续升级；进程重启后一次性补齐所欠档位；一旦有队伍
  承诺即停止该单升级。
- **跨机构转派**：仅已承诺派单可转；交接提出期间责任不悬空，接收方确认后责任才切换；
  支持多次转派并保留全局有序交接链；接收方可拒绝，拒绝后责任仍在原方。
- **隐私授权**：呼叫人按 scope 授权（电话/车牌/精确位置/乘员明细）；读取视图按 scope
  脱敏（未授权字段全掩码），写操作经 `assert_scope` 显式拒绝。

## 运行

运行数据与本地配置不写入源码目录。快照路径用 `--snapshot` 或 `RESCUE_SNAPSHOT` 指定。

```bash
python3 -m service_09251_005 \
  --seed examples/seed.example.json \
  --snapshot /tmp/rescue_state.json \
  --port 8080
```

## API（节选）

| 方法与路径 | 说明 |
| --- | --- |
| `POST /admin/nodes` `/admin/segments` `/admin/teams` `/admin/stations` | 登记道路与救援资源 |
| `POST /admin/roads/close` | 封闭道路，受影响且未承诺的工单自动重规划 |
| `POST /admin/congestion` | 更新服务区占用/排队/离线 |
| `POST /intake` | 受理求援（含风险标志、电池、授权 scope） |
| `GET  /cases/{id}` | 工单视图；`?scope=phone&scope=plate` 控制可见字段 |
| `POST /cases/{id}/location` | 定位更新（只重规划未承诺部分） |
| `GET  /cases/{id}/assignments` `/handovers` | 派单与交接链 |
| `POST /assignments/{id}/claim` | 抢单（并发下唯一赢家） |
| `POST /assignments/{id}/arrive` `/energize` | 到场、补能/排障 |
| `POST /assignments/{id}/transfers` | 发起跨机构转派 |
| `POST /transfers/{id}/ack` `/reject` | 接收方确认/拒绝交接 |
| `POST /cases/{id}/close` | 关单（未完成需求会被拒绝） |
| `POST /tick` | 显式驱动：补偿重试、报价过期重派、超时升级 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：并发抢单（20 竞争者唯一赢家）、时区与跨午夜/工作日/DST 边界、隐私字段授权与
脱敏、补偿失败/退避/重启续跑/转人工、道路单向与封闭、服务区拥堵与离线、重复呼叫风险
合并、责任交接链、全生命周期、超时升级重启补齐，以及 JSON API 与真实 HTTP 适配、
快照往返恢复。

## 编译检查

```bash
python3 -m compileall -q service_09251_005 tests
```
