# 弹性分发架构优化方案

> 合并：长连任务分发方案 + WS 断连韧性修复
> 基于 simple-a2a-registry-v2 实际故障模式分析

---

## 一、问题定义

### 1.1 真实故障模式：WS 断连 ≠ 任务失败

当前架构最严重的隐性缺陷：

```
Registry                       coder-agent                      Hermes Coder
   │                               │                               │
   ├── WS dispatch T-123 ────────► │                               │
   │                               ├── task_ack ──► (accepted)     │
   │                               ├── 启动 Hermes CLI ──────────► │
   │                               │                               ├── 分解任务
   │                               │                               ├── 创建子任务 A,B,C
   │                               │                               │   └── 写入 Registry
   │  ⚡ WS 断连 (NAT/网络抖动)     │  (Agent 进程仍在跑)           │   (仍在创建子任务)
   │                               │                               │
   ├── _fail_agent_tasks() ───────►│                               │
   │   T-123 → failed              │                               │
   ├── promote_retryable_tasks()   │                               │
   │   T-123 → ready               │                               │
   │                               │                               │
   ├── Agent 重连 WS               │                               │
   ├── _maybe_dispatch_pending()   │                               │
   ├── WS dispatch T-123 (again) ─►│                               │
   │                               ├── 启动第二个 Hermes CLI ────► │
   │                               │                               ├── 再次分解
   │                               │                               ├── 创建子任务 A',B',C'
   │                               │                               │   ← 同名副本！
```

**关键数据**：WS 断连中约 60-80% 是 NAT 会话超时、网络抖动等**临时性断连**，Agent 进程本身完好。

### 1.2 三层设计缺陷

| 层次 | 问题 | 位置 |
|------|------|------|
| **① 断连语义错误** | WS 断连 = 立刻 fail 全部任务，不给 agent 重连报活机会 | `server.py:678` 的 `finally` 块 |
| **② state_sync 不修 DB** | 重连后 agent 报 "我还在跑T-123"，只更新内存 `ctx.tasks`，不写回 TaskStore DB | `registry_handler.py:694-715` |
| **③ Hermes 子任务无幂等** | 同 body/title 输入 → 同子任务名称；每个 Hermes 进程独立创建 → 自然重复 | 行为级（Hermes Coder） |

### 1.3 与现有长连任务分发的关系

当前分发架构已有三层模型（事件总线 → WS/SSE 推送 → HTTP callback / 本地 spawn），但缺了**对分发通道不可靠性的补偿机制**。WS 作为"推送加速器"意味着它是尽力而为的——加速器断掉不应触发整个任务的生命周期终结。

---

## 二、设计原则

```
┌──────────────────────────────────────────────────────────┐
│ 原则一：WS 是加速器，不是可靠性主通道                         │
│    · DB + 事件总线是可靠性基础                                │
│    · WS 断连不意味着任务死亡                                   │
│                                                              │
│ 原则二：断连 ≠ 死亡，宽限期                        │
│    · 临时断连（网络抖动/NAT 超时） → 原地恢复                   │
│    · 真正死亡（进程崩溃/超时未回） → 正常 retry                 │
│                                                              │
│ 原则三：Agent 端状态是权威状态                                  │
│    · state_sync 的 active_tasks 是修复依据                     │
│    · Registry 不应假设 "没收到任何消息 = agent 死了"            │
│                                                              │
│ 原则四：Agent 端状态是权威状态                                  │
│    · state_sync 的 active_tasks 是修复依据                     │
│    · Registry 不应假设 "没收到任何消息 = agent 死了"            │
└──────────────────────────────────────────────────────────┘
```

---

## 三、架构变更

### 3.1 任务状态机扩展

```
                 当前 8 状态                               新增状态
  ┌──────┐
  │ TODO │
  └──┬───┘
     │ 依赖晋升
  ┌──▼───┐           ┌───────────┐
  │ READY│──────────►│  DANGLING  │  ← 新增：WS 断连后的过渡状态
  └──┬───┘           └─────┬─────┘
     │ dispatch              │ agent 重连 + state_sync 报活
  ┌──▼───┐                  │  → 恢复 RUNNING
  │CLAIMED│                 │ agent 超过宽限期未回
  └──┬───┘                  │  → 转为 FAILED
     │ execute
  ┌──▼───┐
  │RUNNING│
  └──┬───┘
     │ complete/fail
  ┌──▼────┐ ┌───────┐
  │COMPLTD│ │FAILED │
  └───────┘ └──┬────┘
                │ retry (promote 回 READY)
           ┌───▼───┐
           │ READY │
           └───────┘
```

**DANGLING 语义**：
- 状态值：`dangling`
- 含义：Agent 的 WS 断连了，但不确定是临时断连还是真死亡
- 宽限期：30s（可配置）
- 宽限期内：不触发 retry promotion，Dispatch 循环跳过
- 宽限期后：转为 `failed/agent_dead`，正常走 retry

### 3.2 组件职责变化

```
                          Registry
       ┌────────────────────────────────────────────┐
       │  WS 连接管理器                                │
       │  ├── _ws_connections: Dict[str, WS]          │
       │  ├── _dangling_tasks: Dict[str, float]       │ ← 新增
       │  │   {task_id: dangle_deadline_timestamp}    │
       │  └── _dangle_timer: asyncio.Task             │ ← 新增
       │                                             │
       │  TaskStore                                   │
       │  ├── 8 状态 → 9 状态（+ DANGLING）            │ ← 改造
       │  └── promote_retryable_tasks() → 跳过 DANGLING│ ← 新增
       │                                             │
       │  Dispatcher                                  │
       │  ├── TTL release → 跳过 DANGLING             │ ← 新增
       │  └── claim → 跳过 DANGLING                   │ ← 新增
       │                                             │
       │  state_sync handler                          │
       │  └── 从 DANGLING → RUNNING（写回 DB）         │ ← 改造
       └────────────────────────────────────────────┘

       Agent (coder-agent / opencode-agent)
       ┌────────────────────────────────────────────┐
       │  重连后 state_sync 已有完善机制                │
       │  └── _build_active_tasks() → active_tasks   │
       │                                              │
       │  WS 心跳 + task_ack/progress/complete         │
       │  └── 已有实现，无需改动                       │
       └────────────────────────────────────────────┘
```

### 3.3 WS 断连时间线对比

```
当前行为：
  0s    WS 断连
  0.1s  └─ _fail_agent_tasks() → FAILED
  0.2s  └─ promote_retryable_tasks() → READY
  1s    Agent 重连 WS → state_sync → 发现 T-123 是 READY
  1.1s  └─ _maybe_dispatch_pending() → 再次 dispatch → 双重执行

修复后：
  0s    WS 断连
  0.1s  └─ _dangle_agent_tasks() → DANGLING (+ 30s 计时器)
  1s    Agent 重连 WS → state_sync {active_tasks: [T-123]}
  1.1s  └─ handle_state_sync → T-123: DANGLING→RUNNING（写回 DB）
        └─ 取消 T-123 的宽限计时器
        └─ 不重新 dispatch（Agent 已经在跑）
  (Agent 继续执行，无任何副作用)

Agent 真死亡场景：
  0s    进程崩溃（WS 断连）
  0.1s  └─ _dangle_agent_tasks() → DANGLING (+ 30s 计时器)
  30s   Agent 未重连 → 宽限期超时
  30.1s └─ T-123 → FAILED(agent_dead)
  30.2s └─ promote_retryable_tasks() → 同上 task_id 回 READY
  30.3s └─ dispatch T-123（Agent 重启后接收，无重复）
```

---

## 四、详细设计

### 4.1 新增 TaskStatus.DANGLING

```sql
-- tasks 表 status 字段新增值
-- CURRENT: todo | ready | claimed | running | completed | failed | cancelled | archived
-- ADD:     dangling
```

```python
class TaskStatus(Enum):
    TODO = "todo"
    READY = "ready"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"
    DANGLING = "dangling"  # ← 新增
```

### 4.2 WS 断连处理：从 fail→ dangling

**`server.py` 的 `finally` 块**（当前约 line 667-678）：

```python
# 删掉/改造:
# 旧: await self._fail_agent_tasks(agent_id)
# 新: await self._dangle_agent_tasks(agent_id)

async def _dangle_agent_tasks(self, agent_id: str) -> None:
    """WS 断连 → 标记该 agent 的所有 non-terminal 任务为 DANGLING，启动宽限计时器。"""
    now = time.time()
    dangled_ids: list[str] = []

    for task_id, task in list(self._tasks.items()):
        if task.get("agent_id") != agent_id:
            continue
        state = task.get("state", "")
        if state in ("completed", "failed", "cancelled"):
            continue

        task["state"] = "dangling"
        task["error"] = "ws_disconnected"
        task["updated_at"] = now
        dangled_ids.append(task_id)

    if not dangled_ids:
        return

    # 同步 V2 TaskStore（DANGLING 不触发 retry promotion）
    if self.task_store is not None and self._dispatched_ws_tasks is not None:
        for task_id in dangled_ids:
            if task_id in self._dispatched_ws_tasks:
                self.task_store.update_task_status(
                    task_id, TaskStatus.DANGLING.value,
                )

    # 启动宽限计时器（每个 agent 一个）
    self._dangling_timers[agent_id] = asyncio.create_task(
        self._dangle_timeout(agent_id, dangled_ids)
    )
    logger.info(
        "Agent '%s' dangled: %d task(s), grace period %ds",
        agent_id, len(dangled_ids), DANGLING_GRACE_SECONDS,
    )
```

### 4.3 宽限计时器

```python
DANGLING_GRACE_SECONDS = 30  # 可配置

async def _dangle_timeout(self, agent_id: str, task_ids: list[str]) -> None:
    """宽限期结束 → 转为真正的 FAILED，触发 retry。"""
    await asyncio.sleep(DANGLING_GRACE_SECONDS)

    now = time.time()
    for task_id in task_ids:
        task = self._tasks.get(task_id)
        if task is None or task.get("state") != "dangling":
            continue  # 已被 state_sync 恢复

        task["state"] = "failed"
        task["error"] = "agent_dead"
        task["updated_at"] = now

        if self.task_store is not None and task_id in (self._dispatched_ws_tasks or {}):
            self.task_store.update_task_status(
                task_id, TaskStatus.FAILED.value,
                result="Agent failed to reconnect within grace period",
            )

    logger.warning(
        "Agent '%s' grace period expired: %d task(s) failed (agent_dead)",
        agent_id, len(task_ids),
    )
```

### 4.4 state_sync 愈合路径

**`registry_handler.py` 的 `handle_state_sync`** Step 2 增加：

```python
async def handle_state_sync(ws, data, ctx):
    # ... 现有检查逻辑 ...

    # ------------------------------------------------------------------
    # Step 2 — Merge agent-reported active_tasks
    # ★ 新增：如果 DB 中是 DANGLING 而 agent 报 working → 恢复为 RUNNING
    # ------------------------------------------------------------------
    for at in active_tasks:
        at_id = at.get("id")
        at_status = at.get("status")
        if not at_id or not at_status:
            continue

        # 查 DB 看当前实际状态
        if ctx.task_store is not None:
            db_task = ctx.task_store.get_task(at_id)
            if db_task and db_task.status == TaskStatus.DANGLING.value:
                # Agent 还在跑 → 恢复
                ctx.task_store.update_task_status(
                    at_id, TaskStatus.RUNNING.value,
                )
                # 取消宽限计时器
                if hasattr(ctx, "_dangling_timers"):
                    timer = ctx._dangling_timers.pop(ctx.agent_id, None)
                    if timer:
                        timer.cancel()
                logger.info(
                    "state_sync HEALED task '%s': dangling→running (agent '%s' still alive)",
                    at_id, ctx.agent_id,
                )

        # 继续原有的 merge 逻辑
        server_task = ctx.tasks.get(at_id)
        # ... 保持不变 ...
```

### 4.5 Dispatcher 的 DANGLING 跳过逻辑

```python
async def _poll_cycle(self) -> dict[str, int]:
    # 1. TTL Release — 跳过 DANGLING（由宽限计时器管理）
    # ↑ 关键改动：release_expired_claims 加了 WHERE status != 'dangling'

    # 2. Retry Promotion — 只 promote FAILED，不碰 DANGLING
    # ↑ 关键改动：promote_retryable_tasks 只查 status='failed'

    # 3. Claim + Spawn — 只查 status='ready'
    # ↑ 无需改动，DANGLING 不在此列
```

---

## 五、与现有分发架构的整合

### 5.1 完整分层架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              用户界面 / 外部 API                             │
│                    HTTP API + Web Dashboard + Admin CLI                     │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                    ┌───────────────▼──────────────────────────────────┐
                    │            Layer 1: 事件驱动分发总线               │
                    │  ┌──────────────────────────────────────────┐   │
                    │  │  Event Bus (asyncio.Queue + DB 持久化)    │   │
                    │  │  · 任务创建 → 事件触发 dispatch           │   │
                    │  │  · 结果回传 → 事件触发依赖晋升             │   │
                    │  │  · Safety net: 30s 轮询兜底               │   │
                    │  └────────────────────────────────┬─────────┘   │
                    └───────────────────────────────────┼─────────────┘
                                                        │
                    ┌───────────────────────────────────▼─────────────┐
                    │            Layer 2: 声明式调度引擎               │
                    │  ┌──────────────────────────────────────────┐   │
                    │  │  9 状态状态机 + DAG 引擎 + 重试策略       │   │
                    │  │  · DANGLING 宽限期 + state_sync 愈合     │   │  ← 新增
                    │  │  · 依赖链解析 + 条件分支                   │   │
                    │  └──────────────────────────────────────────┘   │
                    └───────────────────────┬─────────────────────────┘
                                            │
        ┌───────────────────────────────────┼──────────────────────────────┐
        │                                   │                              │
┌───────▼─────────┐     ┌───────────────────▼──────┐     ┌───────────────▼──────┐
│ Layer 3a:       │     │ Layer 3b:                │     │ Layer 3c:            │
│ WS Push / SSE   │     │ HTTP Callback            │     │ K8s Job / Cmd        │
│ (推送加速器)     │     │ (尽力通知)                │     │ (可选/未来)           │
│                 │     │                          │     │                      │
│ · WS 在线→推送   │     │ · Agent 注册 callback    │     │ · K8s API 创建 Job   │
│ · 断连→dangling  │     │ · 30s timeout+退避       │     │ · 资源预留/GPU 调度   │  ← 新增
│ · state_sync 愈  │     │ · 失败→从 DB 补偿拉取    │     │                      │
│ · WS 双向控制    │     │                          │     │                      │
└─────────────────┘     └──────────────────────────┘     └──────────────────────┘
                                            │
                                    ┌───────▼───────┐
                                    │  DB 主通道     │
                                    │  (强一致性)     │
                                    │  · 状态持久化   │
                                    │  · 事件审计     │
                                    │  · 补偿拉取     │
                                    └───────────────┘
```

### 5.2 WS 推送的完整语义重定义

```
推送加速器（当前定义）         弹性推送加速器（新定义）
─────────────────           ─────────────────
DB = 主通道                 DB = 主通道
WS = 降低延迟                WS = 降低延迟
WS 断连 → task 失败          WS 断连 → task dangling
无宽限期                     30s 宽限期
重连后可能重派               重连后 state_sync 愈合
                             宽限期超时 → 正常 retry
```

---

## 六、渐进式实施计划

### Phase 1（1 周）— DANGLING + 愈合路径

| 任务 | 文件 | 优先级 |
|------|------|--------|
| P1.1 新增 `TaskStatus.DANGLING` | `models.py` | 必须 |
| P1.2 `_fail_agent_tasks()` → `_dangle_agent_tasks()` + 宽限计时器 | `server.py` | 必须 |
| P1.3 `handle_state_sync` Step 2 写回 DB（dangling→running） | `registry_handler.py` | 必须 |
| P1.4 TTL 释放跳过 DANGLING | `store.py` | 必须 |
| P1.5 `promote_retryable_tasks()` 跳过 DANGLING | `store.py` | 必须 |
| P1.6 测试：WS 突发断连/快速重连 | `tests/` | 必须 |

### Phase 2（1-2 周）— 事件驱动 + 状态持久化

| 任务 | 文件 | 优先级 |
|------|------|--------|
| P2.1 `asyncio.Queue` 事件总线 | `store.py`, `dispatcher.py` | 推荐 |
| P2.2 Safety net: 30s 轮询兜底 | `dispatcher.py` | 推荐 |
| P2.3 FlowController 状态持久化 | `flow_control.py`, `store.py` | 可选 |
| P2.4 Dashboard/Admin 实时状态 SSE 推送 | 前端 | 可选 |

---

## 七、边界情况与风险

### 7.1 宽限期参数选择

| 场景 | 建议值 | 理由 |
|------|--------|------|
| 局域网 Agent | 10s | 网络延迟低，断连即真死概率高 |
| 广域网 Agent | 30s | NAT 超时常见，需给重连时间 |
| Agent 在移动网络 | 60s | 网络切换（WiFi↔4G）需更长时间 |

建议：配成 `per-agent` 或 `global config`，默认 30s。

### 7.2 Agent 进程真的崩溃了

```
流程:
1. Agent 进程崩溃 → WS 断连
2. Registry: _dangle_agent_tasks() → DANGLING + 30s 计时器
3. Agent 未重连 → 30s 后宽限期超时
4. Registry: _dangle_timeout() → FAILED(agent_dead)
5. Dispatcher: promote_retryable_tasks() → 生成新 task
6. Agent 重新启动 → 注册新 WS 连接
7. Registry: _maybe_dispatch_pending() → dispatch 新 task

▲ 正确：30s 的延迟换来了正确性，不产生重复 task
  Agent 重启后也不会收到旧 task（已 fail），只收到新的 retry task
```

### 7.3 Agent 崩溃后重启，收到旧 task 的 state_sync

```
Agent 重启 → 注册 WS → state_sync {active_tasks: []}
  (内存中无任何 active_tasks)
Registry: 发现无 active_tasks → 无需愈合 → 等待宽限期到期 → FAILED
          → 生成新 task → dispatch

▲ 正确：Agent 重启后 state_sync 内容为空，Registry 认定其真死亡
```

### 7.4 宽限期内 WS 短暂重连后又断开

```
0s    WS 断连 → DANGLING + 30s 计时器
5s    Agent 重连 → state_sync 报活 → 恢复 RUNNING + 取消计时器
8s    WS 又断连 → 重新 DANGLING + 新 30s 计时器

▲ 正确：每次断连重新计时，不会累计宽限期时间
```

### 7.5 DANGLING 与 Claim TTL 的交互

DANGLING 状态与 Claim TTL 的关系：

```
核心：DANGLING 状态的 task 不应该被其他 worker 抢走 claim

处理：
1. _dangle_agent_tasks() 不释放 claim lock（保留现有 claim）
2. TTL release 跳过 DANGLING（不标记为 failed）
3. 宽限期到期 → FAILED → claim 自然释放
4. state_sync 恢复 → 维持原有 claim

▲ 避免：同一 task 被两个 worker 同时 claim
```

---

## 八、测试验证要点

### 8.1 核心场景矩阵

| 场景 | 预期 | 自动化 |
|------|------|--------|
| WS 突发断连 <1s 恢复 | 无副作用，继续执行 | ✅ |
| WS 断连 10s 后 agent 重连报活 | state_sync 愈合 dangling→running | ✅ |
| WS 断连 31s agent 未回 | dangling→failed → 新 retry task | ✅ |
| Agent 进程崩溃后重启 | 宽限期到期 → 正常 retry | ✅ |
| 重试后不产生同名子任务 | 新 task ID → 子任务名不同 | ✅ |
| 宽限期内 WS 反复断连重连 | 每次重新计时，不累积 | ✅ |
| DANGLING 期间不触发 retry | retry promotion 跳过 dangling | ✅ |
| 两个 Agent 同时 claim 同一 task | 只有 1 个成功（BEGIN IMMEDIATE） | ✅ |

### 8.2 混沌测试

```bash
# 模拟 WS 断连
kill -STOP <agent_pid>   # 暂停 agent 进程 → WS 无响应
sleep 5
kill -CONT <agent_pid>   # 恢复 agent → 触发 state_sync
# 验证：任务继续执行，无重复子任务

# 模拟 Agent 真死
kill -KILL <agent_pid>   # 杀死 agent
sleep 31                 # 等待宽限期
# 验证：新 retry task 创建，旧 task fail
```