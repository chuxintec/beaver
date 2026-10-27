# v0.2 使用与交接

先 `project inspect` 确认 Unity 版本；用 `prefab.inspect` 读取实际节点路径和组件。路径相对 Prefab 根，不凭文件名推断层级。每个输出目录必须是新目录；计划 SHA 来自 `plan-identity.json` 或命令结果。

```sh
beaver batch plan --project /absolute/project --spec /absolute/window-group.json --out /absolute/plan
beaver batch apply --plan /absolute/plan/batch-plan.json --expected-plan PLAN_SHA256 --out /absolute/run --editor-path /absolute/Unity
beaver batch recover --transaction /absolute/run --out /absolute/recovery --editor-path /absolute/Unity
```

`recover` 要求原事务固定的 beaver 实现。开发版或旧 v0.1 事务用其原 wheel/执行器；不要换版本重放。执行失败可能已经保存部分步骤，先读 journal/checkpoint；人工后改、未知写集外变化、缺失可信 checkpoint 都会阻止恢复。

例如已有窗口拥有 `Body/Viewport/Content`、inactive 模板 `Body/Template/Text` 和现有 `WindowController.Close()` 时，可使用下列结构；请将路径与完整类型名替换为该工程原生 inspect 的结果，不能凭此创建业务方法。

```json
{
  "schemaVersion": 2,
  "operation": "batch",
  "runtimeRoot": "Assets/Game/Scripts/BeaverTools",
  "steps": [{
    "kind": "prefab.patch",
    "assetPath": "Assets/Game/Content/UI/Window.prefab",
    "actions": [
      {"kind":"ui.scroll","path":"Body","targetNode":"Body/Viewport","parent":"Body/Viewport/Content","mode":"vertical"},
      {"kind":"ui.grid","path":"Body/Viewport/Content","numbers":[360,64,0,8],"index":1},
      {"kind":"ui.list","path":"Body","parent":"Body/Viewport/Content","targetNode":"Body/Template","property":"Text","items":["First","Second"]},
      {"kind":"ui.event","path":"Close","property":"m_OnClick","targetNode":"","targetComponent":"Example.WindowController","method":"Close"}
    ]
  }]
}
```

模板必须明确 inactive，并已绑定字体；列表仅销毁自己拥有的行。Grid 的内容高度可由明确的 `ContentSizeFitter.m_VerticalFit=enum 2` 适配。静态诊断的空 Sprite 可能是有意纯色面板，需开发者判断。Variant 支持属性、事件和新增 child override，继承结构修改会给出单独 base 计划或新增 child 的替代说明，不自动采用替代方案。

```sh
beaver references --project /absolute/project --out /absolute/graph --cache /absolute/reference-cache.json
```

图给出直接/反向 GUID 依赖、可证明的 C# 加载字面量 span 及未知动态键。JSON 字符串只是观察结果，需显式 JSON pointer 映射；C# 使用 `script.key` 的精确 span/before/after。`reference.replace` 须列完整原生直接消费者，`asset.move` 保留 GUID/localID。运行时动态表达式、Spine 附件/区域、BMFont 闭包、语义级代码重构不自动猜替换。

```sh
beaver debug start --manifest /absolute/build/artifact-manifest.json --expected MANIFEST_SHA256 --out /absolute/session --duration 600 --width 900 --height 1400
beaver debug inspect --session /absolute/session/session.json
beaver debug tail --session /absolute/session/session.json --lines 40
beaver debug stop --session /absolute/session/session.json
beaver debug target --transaction /absolute/editor-run
```

宽高可省略（遵循 PlayerSettings），提供时必须成对且各为64～8192；请求尺寸不等于窗口/DPI实测。worker 仅管理自己的实际子进程，最多一小时，无自动重启。会话存在不代表运行或业务成功；读取 status、实际 App 结果及截图。日志只读末尾最多256KiB并脱敏。`debug screenshot` 只登记调用者提供图片的哈希与来源，不认证其为当前屏幕。

正式游戏必须自行适配独立应用身份、启动场景、账号/存档和服务隔离；本工具不会自动获得这类隔离。本轮真实 UI 验证是专用 fixture，未接管游戏启动或正式业务。

## GREA 交接

JSON 外层仍为 `beaver-result/1`，capabilities 外层仍为 `beaver-capabilities/1`。新增 `beaver-batch/2`、`beaver-batch-transaction/2`、`beaver-reference-graph/2`。CLI 失败状态与退出码一致，包装层必须保留 partial/blocked/failed/not_run，不能由进程 exit0 推断业务通过。

通用薄调用可传以上参数；现有 GREA 的 provider 版本允许范围和固定0.1绑定仍需 GREA 负责人明确升级后才能接受0.2，本项目未改其代码、配置或冻结执行链。不要静默重指旧候选或旧事务。大 Unity 制品新增显式 `artifactPolicy=beaver-unity-build/2`（单4GiB/总8GiB）；单独核验使用 `payload verify --unity-artifact`，普通采用/物化仍单512MiB。

## 自然使用记录

v0.1 的 `prefab.edit` 仅标量/对象字段、拒绝 Variant，普通编辑失败无通用恢复；跨窗口动作需拆计划/Editor 调用或另写一次性脚本。v0.2 的同一5-step真实组在一次 Editor 中完成详情、图鉴、Variant、Sprite替换与移动，用原生方法绑定已有业务； fixture 仅负责离线数据呈现和证据，未成为产品业务。

真实组计划对54288个源路径做元信息盘点，只哈希1781个相关输入（13806377字节）；执行一次 Editor，12.52秒，Python1789/native1841次哈希读取，声明10条写路径、实际7路径变化、写集外0。只读图首次34926项hash/parse为11.27秒；次轮34926个cache hit、hash/parse0，为5.07秒。两者仍需目录盘点且保留2个未知动态键为partial；这些是同机自然记录，不外推为通用速度倍数。完整构建继续全源绑定，并单独报告启动期字体规范化导致的失败重试。
