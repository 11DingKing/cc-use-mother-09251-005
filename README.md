# 新能源道路救援派单

本项目用于建设面向业务人员的纯服务端系统。代码按领域模型、应用服务、持久化与接口边界组织；时间、标识和外部输入应通过可替换端口接入，以便稳定复现状态变化。运行数据与本地配置不得写入源码目录。

## 结构

```
service_09251_005/
  domain/models.py            # 求援单、派单、救援队、值班窗口、道路封闭、充电站
  application/services.py     # 受理合并、派单引擎、抢单/转派/到场/补能/关闭、超时升级
  application/ports.py        # 时钟、标识、通知等可替换端口
  application/privacy.py      # 隐私字段授权与脱敏
  infrastructure/repository.py# SQLite 持久化（原子抢单、交接链、升级进度可恢复）
  interfaces/http_api.py      # 标准库 HTTP 接口
  app.py                      # 组合根；数据目录取 RESCUE_DATA_DIR，默认系统临时目录
```

## 行为要点

- **派单**：按道路方向、队伍驻点距离、能力匹配、值班窗口（本地时区、支持跨午夜）与优先级计算候选；低电量警情联动服务区拥堵——可达站点全部拥堵时偏好移动充电队，否则给出 `GUIDE_TO_SITE:<站点>` 建议。
- **只调整未承诺部分**：定位更新或道路封闭仅触发未抢单派单的重算；已抢单（已承诺）派单一律不动。
- **合并不吞风险**：重复呼叫按同电话或同路段相近位置在时间窗内合并，风险取最高、电量取最低，来话原文写入审计事件。
- **跨机构转派**：每次转派追加责任交接记录（双方机构、队伍、原因、时间），补偿失败时保留悬挂状态并落 `compensation_failures` 台账。
- **超时升级**：未承诺派单按优先级 SLA 升级并扩大候选范围，升级进度持久化，服务重启后继续升级。
- **隐私**：姓名、电话、精确坐标按 `privacy:read` 权限范围脱敏；接口凭 `X-Operator-Token` 鉴权。

## API 摘要

`POST /api/requests` 受理 · `POST /api/requests/{id}/location` 定位更新 · `POST /api/orders/{id}/claim` 抢单 · `POST /api/orders/{id}/transfer` 转派 · `POST /api/orders/{id}/arrive` 到场 · `POST /api/orders/{id}/recharge` 补能 · `POST /api/orders/{id}/close` 关闭 · `POST /api/roads/closures` 道路封闭 · `POST /api/teams`、`POST /api/charging-sites` 登记 · `POST /api/escalations/sweep` 升级扫描

默认令牌：`dispatcher-token`（读写+隐私明文）、`team-token`（读写、隐私脱敏）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖并发抢单、时区边界、隐私字段授权、补偿失败、重复呼叫合并、道路封闭重派、超时升级重启恢复与接口全生命周期。

## 编译检查

```bash
python3 -m compileall -q service_09251_005 tests
```
