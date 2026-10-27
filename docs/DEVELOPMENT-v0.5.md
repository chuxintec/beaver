# UI 批量检查、接线与诊断

原 v0.4 能力归入 0.5.0 全量整合；既有 toolkit 0.4.0 的固定包和事务保留。Unity 6000.6.2f1 原生采集和执行须选定项目与 Editor。以下命令均是项目主开发在其授权范围内运行；工具不自动接管正式工程。

1. `beaver ui inspect --project /project --spec /ui-spec.json --editor-path /Unity --out /inspection` 原生读取显式 Prefab 或 Scene。`static-prefab`、`static-scene` 与真实 Play 状态分开；`ui analyze --snapshot /snapshot.json --spec /ui-spec.json --out /analysis` 的输入标记为调用者提供，不能认证采集来源。规则与预期必须有来源，不把空事件、条件隐藏或禁用按钮自动当作错误。
2. `beaver wiring plan --project /project --snapshot /inspection/ui-snapshot.json --spec /wiring.json --out /wiring-plan` 用已编译字段类型、唯一候选或显式角色，以及资源 GUID/localID 生成接线计划。无法唯一确定目标时整组不生成可执行计划。
3. 接线结果的 `batch.report` 与 `batch.planSha256` 直接交给既有 `beaver batch apply --plan /batch-plan.json --expected-plan SHA --editor-path /Unity --out /transaction`。原生执行再次核对字段类型和现值；新引用必须为空，非空引用只接受显式 `replace:true`。Button.onClick 仅支持空持久事件到真实现有组件的 public instance void() 方法；不生成空方法，不覆盖已有监听。
4. `beaver diagnostics summarize --input /transaction/journal.json --input /provider-result.json --input /editor.log --out /diagnostic --limit 100` 保留证据哈希、位置、阶段、真实退出码与写入状态。执行后外层 Schema 拒收须保留原始回执，修消费者后离线重验，不重放已写事务。

接线 spec 示例（路径与类型须来自本工程原生采集）：

```json
{
  "schemaVersion": 1,
  "bindings": [
    {
      "asset": "Assets/UI/Panel.prefab",
      "nodePath": "",
      "componentType": "Game.Panel",
      "field": "submit",
      "targetRole": "submit",
      "source": "批准的窗口交互规格：提交按钮"
    }
  ],
  "roles": [{"role":"submit","target":{"nodePath":"Submit","componentType":"UnityEngine.UI.Button"}}]
}
```

单值 Sprite/TMP/View 引用使用相同字段合同。资源目标是精确的 `{"guid":"...","localId":21300000}`，其中数字必须由原生子资源身份发现得出，不能抄示例猜测。数组、复杂泛型、动态方法和持久化 Scene 接线不在本版支持子集内；Scene 仍可检查。

明确布局/字号修正用 `ui repair-plan --project /project --snapshot /snapshot.json --spec /repairs.json --out /plan`。输入是 `{"schemaVersion":1,"repairs":[...]}`，每项明确 `asset/nodePath/componentType/field/mode/value/source`；支持 scalar/vector2/color 的已采集序列化字段。禁止身份及对象引用绕过接线保护。相同 repairs 数组也可放在 wiring spec 中，和接线合为一个已有 batch 事务。布局预期由 Agent 给出，工具不从静态规则自动决定美术设计。

快照源文件及 meta 必须保持相同哈希；正式写入前继续核 batch 依赖和并发保护。Variant/nested Prefab 沿既有事务的保存边界，不 Unpack、不修改未列入计划的基 Prefab。保存和重载证明序列化状态，不证明实际画面、物理点击或业务验收。

统一诊断的 `unknown`、候选差异及未运行阶段原样保留；CLI 对未知执行或执行后外层拒收返回 partial，完整信息留在 diagnosticReport。`beaver tool <id>` 保持旧 toolkit 原始输出与退出码，不使用 beaver-result 信封；toolkit 可选依赖由显式 extra 或既有 GREA 运行时提供。

实际采集的肯定验证与边界见[本地验证索引](VALIDATION-v0.5.json)。原生Runtime Collect的按钮中心射线只是一次采样；输入的classification区分failure、rule_difference、observation，未知信息不会转成通过。
