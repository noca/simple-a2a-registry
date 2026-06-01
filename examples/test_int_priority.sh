#!/bin/bash
# ============================================================
# 测试 A2A Registry 整数优先级 (INT priority) 功能
# ============================================================

BASE="http://localhost:8321"

echo "=== 1. 创建不同优先级的任务 ==="

# 高优先级 (priority=10)
T1=$(curl -s -X POST "$BASE/v2/tasks" \
  -H "Content-Type: application/json" \
  -d '{"title":"高优先级任务","body":"priority=10","priority":10}' | jq -r '.task.id')
echo "创建高优先级任务: $T1 (priority=10)"

# 中优先级 (priority=5)
T2=$(curl -s -X POST "$BASE/v2/tasks" \
  -H "Content-Type: application/json" \
  -d '{"title":"中优先级任务","body":"priority=5","priority":5}' | jq -r '.task.id')
echo "创建中优先级任务: $T2 (priority=5)"

# 低优先级 (priority=1)
T3=$(curl -s -X POST "$BASE/v2/tasks" \
  -H "Content-Type: application/json" \
  -d '{"title":"低优先级任务","body":"priority=1","priority":1}' | jq -r '.task.id')
echo "创建低优先级任务: $T3 (priority=1)"

# 默认优先级 (不传 priority → 默认 0)
T4=$(curl -s -X POST "$BASE/v2/tasks" \
  -H "Content-Type: application/json" \
  -d '{"title":"默认优先级任务","body":"不传priority"}' | jq -r '.task.id')
echo "创建默认优先级任务: $T4 (不传 → 默认0)"

echo ""
echo "=== 2. 查询任务详情（验证 priority 字段）==="
for t in $T1 $T2 $T3 $T4; do
  result=$(curl -s "$BASE/v2/tasks/$t" | jq '{id:.id, title:.title, priority:.priority, status:.status}')
  echo "$result"
done

echo ""
echo "=== 3. 按优先级降序排序 \`-priority\`（高→低）==="
curl -s "$BASE/v2/tasks?sort=-priority&limit=10" | jq '.tasks[] | {title, priority}'

echo ""
echo "=== 4. 按优先级升序排序 \`priority\`（低→高）==="
curl -s "$BASE/v2/tasks?sort=priority&limit=10" | jq '.tasks[] | {title, priority}'

echo ""
echo "=== 5. 更新优先级 ==="
curl -s -X PATCH "$BASE/v2/tasks/$T3" \
  -H "Content-Type: application/json" \
  -d '{"priority": 99}' | jq '{id:.id, title:.title, priority:.priority}'

echo "验证更新后排序:"
curl -s "$BASE/v2/tasks?sort=-priority&limit=5" | jq '.tasks[] | {title, priority}'

echo ""
echo "=== 6. 负值优先级测试 ==="
T5=$(curl -s -X POST "$BASE/v2/tasks" \
  -H "Content-Type: application/json" \
  -d '{"title":"负优先级任务","body":"priority=-5","priority":-5}' | jq -r '.task.id')
echo "创建负优先级任务: $T5 (priority=-5)"

curl -s "$BASE/v2/tasks?sort=priority&limit=10" | jq '.tasks[] | {title, priority}'

echo ""
echo "=== 7. 边界值测试 ==="
T6=$(curl -s -X POST "$BASE/v2/tasks" \
  -H "Content-Type: application/json" \
  -d '{"title":"大数值优先级","body":"priority=999999","priority":999999}' | jq -r '.id')
echo "创建大数值优先级任务: $T6 (priority=999999)"
curl -s "$BASE/v2/tasks/$T6" | jq '{title, priority}'

echo ""
echo "=== 清理: 删除测试任务 ==="
for t in $T1 $T2 $T3 $T4 $T5 $T6; do
  curl -s -X DELETE "$BASE/v2/tasks/$t" | jq '{status:.status, id:.id}'
done

echo ""
echo "=== 测试完成 ==="