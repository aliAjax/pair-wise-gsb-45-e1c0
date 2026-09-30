# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数（`state=reschedule_pending`查看待改期计划）。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data`必填`channel`（航道）；与活跃封锁冲突的计划会被拒绝。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`；`action`新增`reschedule`（待改期计划人工改期，需给出避开封锁的新窗口）。
- `POST /api/closures`：登记封锁，请求体为`{"reference":"CLO-1","data":{"kind":"berth_outage|channel_closure","target":"B12","start_hour":8,"end_hour":20,"reason":"..."}}`；同泊位/同航道的草稿、已确认计划进入待改期，已靠泊、已离泊保持原状态。
- `GET /api/closures`、`GET /api/closures/{id}`：封锁列表与详情，详情含每个受影响计划的改期原因、恢复状态和受影响数量。
- `GET /api/closures/{id}/audit`：封锁时间线。
- `POST /api/closures/{id}/actions/adjust`：调整封锁时段，新撞上的计划失效待改期，不再被覆盖的自动改期计划恢复原状态。
- `POST /api/closures/{id}/actions/revoke`：撤销封锁，自动恢复未被人工改期的计划；人工改期过的不回退。

封锁登记/调整/撤销与计划状态、审计事件在同一事务写入，失败整体回滚，可直接重试；`reference`唯一约束防止重复提交。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及封锁登记/改时段/撤销与计划联动的恢复调度、失败回滚重试。
