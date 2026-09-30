# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突、航道冲突与封锁重叠判定。
- `src/repository.py`：SQLite建表/迁移、事务、幂等键和查询。
- `src/service.py`：用例编排、权限检查、封锁联动的可恢复重调度、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、封锁联动、原子性与幂等、失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表并对旧库做轻量迁移。

## 计划与调度模型

靠泊计划创建时必须同时指定泊位`berth`与航道`channel`；同泊位**或**同航道的计划时间窗
不可重叠。时间轴以小时为单位，允许跨日（0~720）。

计划状态：

| 状态 | 含义 |
| --- | --- |
| `draft` / `confirmed` | 草稿 / 已确认的正常计划 |
| `reschedule_pending` | 撞上封锁且系统找不到可行窗口，计划**失效待改期**，只能人工改期 |
| `auto_rescheduled` | 系统自动避让到新窗口；封锁缩短/撤销后**可自动恢复原窗口** |
| `manual_rescheduled` | 调度员人工改期落定；封锁变化**不回退**，可继续确认/靠泊 |
| `berthed` / `departed` / `cancelled` | 已靠泊 / 已离泊 / 已取消，封锁联动一律不动 |

每个计划携带`schedule_info`：改期方式(`mode`)、改期原因(`reason`)、改期前状态
(`prior_state`)、原窗口(`original_eta_hour/etd_hour`)、关联封锁、是否可自动恢复
(`reschedulable`)。列表、统计、时间线均以这份结果为准。

## 封锁与联动规则

封锁分两类：`berth_outage`（泊位停用，按`berth`匹配）和`channel_closure`
（航道封控，按`channel`匹配）。

- **登记封锁**：同泊位/同航道的`draft`、`confirmed`计划先尝试自动避让到封锁结束后的
  最早可行窗口（同时避开其他封锁和同泊位/同航道计划）；找不到可行窗口则进入
  `reschedule_pending`。已靠泊/离泊/取消计划保持原状态。
- **封锁时段变化**：原窗口被让出来的自动改期计划恢复原窗口；原窗口仍冲突的按新封锁
  结束点重新避让；避让无空档则失效待改期。
- **封锁缩短/撤销**：**只恢复自动改期的计划**；人工改期过的计划锁定在人工窗口，
  不跟着回退。`reschedule_pending`也不会被自动恢复，必须人工改期。
- **人工改期后的计划又撞上新封锁**：计划失效进入`reschedule_pending`（安全优先），
  但撤销新封锁时不会自动回退。

所有封锁写操作（封锁记录 + 受影响计划改期 + 计划/封锁双方审计 + 幂等记录）在同一个
SQLite `BEGIN IMMEDIATE`事务内提交，任一写入失败整体回滚，不会留下半套结果。

写接口支持`Idempotency-Key`请求头：同键+同请求体重试直接返回首次结果，不重复改期；
同键不同请求体返回409。

## 主要接口

计划：

- `GET /api/records`：计划列表，可带`state`和`limit`。
- `GET /api/records/{id}`：计划详情（含`schedule_info`）。
- `GET /api/records/{id}/audit`：计划审计时间线。
- `POST /api/records`：创建计划，`{"reference":"...","data":{...}}`，`data`需含`channel`。
- `POST /api/records/{id}/actions/{action}`：`confirm`/`berth`/`depart`/`cancel`，
  请求体`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/actions/reschedule`：人工改期，
  `{"expected_version":3,"data":{"eta_hour":30,"etd_hour":42,"reason":"..."}}`，
  返回`{"record":...,"recovery_status":"manual_locked","affected_count":1}`。

封锁：

- `POST /api/blockades`：登记封锁并联动改期，
  `{"data":{"kind":"berth_outage|channel_closure","target":"B12","start_hour":8,"end_hour":20,"reason":"检修"}}`。
- `POST /api/blockades/{id}/window`：调整封锁时段并触发重算，
  `{"data":{"start_hour":8,"end_hour":10}}`。
- `POST /api/blockades/{id}/revoke`：撤销封锁，自动改期计划恢复。
- `GET /api/blockades`：封锁列表，可带`status`、`kind`、`limit`。
- `GET /api/blockades/{id}`：封锁详情。
- `GET /api/blockades/{id}/audit`：封锁审计时间线（含其触发的计划改期事件）。

封锁写接口统一返回：

```json
{
  "blockade": {"id": 1, "kind": "channel_closure", "target": "C1", "status": "active"},
  "items": [
    {
      "record_id": 7, "reference": "VOY-1", "state": "auto_rescheduled", "version": 2,
      "reschedule_reason": "航道封控导致原窗口不可用，系统自动改期避让",
      "recovery_status": "auto_rescheduled",
      "recovery_message": "原窗口撞上航道封控，已自动避让，封锁解除后可恢复",
      "reschedulable": true, "mode": "auto",
      "original_window": {"eta_hour": 6, "etd_hour": 18},
      "current_window": {"eta_hour": 20, "etd_hour": 32},
      "affected_count": 1, "related_blockade_ids": [1]
    }
  ],
  "affected_count": 1
}
```

`recovery_status`取值：`auto_rescheduled`（自动避让）、`auto_reshifted`（封锁时段
变化后重新避让）、`restored`（已恢复原窗口）、`pending`（无空档，失效待改期）、
`auto_failed_pending`（重算时避让失败）、`manual_invalidated`（人工窗口再撞锁失效）、
`manual_locked`（人工改期落定）。

其他：

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/stats`：统计，返回`{"records":{状态:数量},"blockades":{状态:数量}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`；封锁登记/改期/
撤销与人工改期需要`port_controller`（或`admin`）角色。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖完整流程、规则计算、航道/泊位冲突、两类封锁联动、缩短重避让、撤销恢复、人工
改期不回退、待改期、权限拒绝、版本冲突、事务回滚与幂等重试。
