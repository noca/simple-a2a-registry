# WS Task Lifecycle Management

> 构建一套完整的 agent WS 任务生命周期管理，覆盖实时心跳、实时任务数据、异常检测与任务状态恢复。

## 一、问题定义

### 1.1 现有缺口

| 问题 | 描述 | 影响 |
|------|------|------|
| 无实时心跳 | HTTP heartbeat 30s 间隔，无 task 状态关联 | 服务端无法实时感知 agent 任务执行状态 |
| 无实时任务数据 | task_progress 仅单向（agent→server），无 pull 机制 | WebUI 无法主动查询进行中任务详情 |
| 孤任务 | Agent 崩溃后未发 task_result，服务端无检测 | 任务永久 stuck 在 "working" 状态 |
| 无可配置超时 | 无 task 级别 timeout 机制 | 长耗任务卡死无兜底 |
| V1/V2 隔离 | Dispatcher WS 派发不写入 V1 task 表 | V1 Tasks 页面查不到进行中的 V2 任务 |
| 重连状态丢失 | Agent 重连后无主动 pending 任务请求 | 完全依赖服务端 `_maybe_dispatch_pending()` 单次尝试 |

### 1.2 核心需求

1. **WS 双向心跳携带任务状态** — Agent ping 带上当前 task 信息，Server pong 带上 pending 任务
2. **任务超时检测** — Server 侧可配置 task timeout，超时未完成自动 fail
3. **WS 断连自动失败** — Agent WS 断开后服务端自动标记其进行中任务为 failed
4. **重连状态恢复** — Agent 重连后主动请求 pending 任务 + 报告未完成的 task
5. **实时任务数据推送** — task_progress server 可主动推送至 Admin UI 和 WebSocket 订阅者

## 二、架构设计

### 2.1 WS 消息协议扩展

#### Agent → Server

```json
// ping — 携带任务状态
{"type":"ping","ts":1717000000,"active_task":"task-uuid","task_status":"working","task_progress":0.3}

// task_ack — 确认接收任务
{"type":"task_ack","id":"task-uuid","status":"accepted","started_at":1717000000}

// task_progress — 进度更新
{"type":"task_progress","id":"task-uuid","status":"working","progress":0.5,"message":"Compiling..."}

// task_complete — 任务完成
{"type":"task_complete","id":"task-uuid","status":"completed","result":{...},"metrics":{"duration":12.5,"output_size":1024}}

// task_fail — 任务失败
{"type":"task_fail","id":"task-uuid","status":"failed","error":"Timeout","code":"ERR_TIMEOUT"}

// state_sync — 重连后恢复
{"type":"state_sync","agent_id":"uuid","active_tasks":[{"id":"task-uuid","status":"working","started_at":1717000000}]}
```

#### Server → Agent

```json
// pong — 携带 pending 任务
{"type":"pong","ts":1717000000,"pending_tasks":[{"id":"t_xxx","title":"...","body":"..."}]}

// task_cancel — 服务端取消任务
{"type":"task_cancel","id":"task-uuid","reason":"timeout","dispatched_at":1717000000}

// task_sync_reply — state_sync 回复
{"type":"state_sync_reply","orphaned_tasks":[{"id":"task-uuid","status":"completed","result":{...}}]}

// health_check — 主动健康探测
{"type":"ping","ts":1717000000}
```

### 2.2 任务状态机

```
                   +---------+
                   | PENDING |  (从未派发)
                   +----+----+
                        |
                   +----v----+
                   |DISPATCH |  (服务端已发到 WS)
                   +----+----+
                        |
                   +----v----+
                   |ACCEPTED |  (agent 回复 task_ack)
                   +----+----+
                        |
                   +----v----+
                   |WORKING  |  (agent 确认正在执行)
                   +----+----+
                    /        \
            +-----v--+    +--v------+
            |COMPLETED|    | FAILED  |
            +--------+    +---------+
                          (含: timeout / error / disconnected)
```

### 2.3 组件变更

#### Server 侧

```
RegistryHandler
├── _tasks: Dict[str, Task]               // 升级为带超时的 Task 对象
├── _ws_connections: Dict[str, WS]         // 不变
├── _task_timeout: int                     // 新增：task 超时秒数（默认 300）
├── _ws_ping_task: asyncio.Task            // 新增：服务端主动 ping 定时器
├── _orphan_detector_task: asyncio.Task    // 新增：孤儿任务扫描
├── handle_ws()                            // 改造：消息路由支持新类型
├── _schedule_task_timeout()               // 新增：注册 task 超时定时器
├── _fail_agent_tasks()                    // 新增：失败 agent 所有进行中任务
└── _handle_state_sync()                   // 新增：处理重连状态同步
```

#### Agent 侧（编码器 + OpenCode）

```
// 共有改动
├── 定时 ping 携带 active_task 信息
├── process_ws_task() 增加 task_ack
├── process_ws_task() 增加 task_progress 频率
├── 重连后发送 state_sync
└── 接收 task_cancel 处理
```

### 2.4 核心算法

#### 任务超时检测

```
1. dispatch 时创建超时定时器（dispatch_at + timeout）
2. 收到 task_ack → 重设定时器（ack_at + timeout）
3. 收到 task_progress → 重设定时器（now + timeout）
4. 定时器触发 → 标记 task failed("timeout") → 发 task_cancel → 清理
```

#### WS 断连处理

```
1. WS 连接的 finally 块（handle_ws:701-708）
2. 扫描 self._tasks 中该 agent 的所有 working/accepted 状态任务
3. 标记为 failed("agent_disconnected")
4. 同步更新 V2 TaskStore（如适用）
5. 发通知到 Admin UI
```

#### 重连状态同步

```
1. Agent 重连 WS 后先发 {"type":"state_sync",...}
2. Server 查数据库找出该 agent 已 dispatch 但未完成的 task
3. 与 agent 汇报的 active_tasks 做 merge
4. 服务端认为已完成但 agent 不知的 → 发 task_sync_reply
5. Agent 认为已完成但服务端不知的 → agent 补发 task_complete
```

## 三、实现计划

### Phase 1 — 核心生命周期（本周）
- [ ] P1.1 扩展 WS 消息协议：task_ack, task_complete, task_fail, state_sync
- [ ] P1.2 服务端 task 超时检测 + WS 断连失败
- [ ] P1.3 Agent 侧发送 task_ack + 定期 task_progress
- [ ] P1.4 Agent 重连后 state_sync
- [ ] P1.5 测试验证

### Phase 2 — 实时数据（下周）
- [ ] P2.1 Agent ping 携带 active_task 信息
- [ ] P2.2 Server pong 携带 pending_tasks
- [ ] P2.3 task_progress 推送到 Admin UI
- [ ] P2.4 WebUI 实时任务状态面板

### Phase 3 — 弹性与治理（后续）
- [ ] P3.1 可配置 task_timeout per agent
- [ ] P3.2 自动重试策略（max_retries + backoff）
- [ ] P3.3 任务审计日志
- [ ] P3.4 熔断机制（连续失败暂停派发）

## 四、影响评估

### 向后兼容
- 新增消息类型（task_ack, task_complete, task_fail, state_sync）不会破坏旧 agent
- 旧 agent 不发送这些消息，服务端按原有逻辑处理（忽略未知类型）
- `{"type":"task_progress"}` 和 `{"type":"task_result"}` 继续兼容

### 性能
- 新增的 ping/pong 携带数据量极小（<1KB）
- task 超时检测使用 asyncio.create_task 定时器，O(n) agent 连接数
- 断连扫描 O(m) 该 agent 的活跃 task 数
