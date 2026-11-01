# 架构、配置与更新总览

[English](deployment.md) · [HA 安装与运维](home-assistant.md) · [规则管理](monitoring.md)

Video Event Monitor 在 HA 上聚合事件、筛选历史画面；CMA 在云端运行分析 Worker。
需要群通知时，由已有 NyxBot 读取核验后的结果并发送总结。这三部分分别配置和更新。

## 名称与部署位置

| 名称 | 含义 | 运行位置 |
| --- | --- | --- |
| **Video Event Monitor** | 产品名称、HA 插件显示名称 | HA |
| `frigate-cma-events` | 本仓库名称、核心镜像包名 | 上述 HA 插件内部 |
| `cma-event-pipeline` | `nyx-homeassistant-node` 中保留的目录和插件 slug | 同一个 Video Event Monitor 实例 |
| Home Assistant Event Relay | 订阅 MQTT/HA 事件，过滤并可靠转发 | HA 上的独立插件 |
| NyxID Node | 按授权代理 HA/局域网 HTTP 服务、注入本地凭证 | HA 上的独立插件 |
| CMA 事件通道与 Worker profile | 配置事件路由、模型、提示词和权限 | CMA 云端 |
| 已有 NyxBot 与可选通知自动化 | 查结果、生成总结、发到指定聊天 | NyxID 与相应聊天平台 |

前三个名称对应同一个监控程序。仓库、镜像和 slug 保留原地址，界面和说明统一使用
Video Event Monitor。NyxID 中的多个 service 条目用于不同权限的访问入口，
创建 service 条目不会额外部署一个应用容器。

[HA 仓库](https://github.com/ChronoAIProject/nyx-homeassistant-node)维护插件包装、
Relay 和节点程序；本仓库维护监控核心及通用示例。Worker、通道和通知自动化是平台配置。
[餐厅场景](../examples/dining/README.md)是通用监控服务的一条应用示例。
[云端聚合部署](../deploy/README.md)是另一种放置方式，HA 方案无需同时部署那套应用。

## 事件消费链路

```text
家中 HA
摄像头 -> Frigate -> MQTT review 事件 -> Event Relay
                                            |
                                      认证后的事件元数据
                                            v
                                  Video Event Monitor
                              规则 / 聚合 / 去重 / 频率限制
                                            |
                                        签名候选事件
                                            v
CMA 云端                             自定义事件通道
                                    profile + per_event
                                            |
                                        分析 Worker
                                            |
          +---------------------------------+------------------------+
          |                                 |                        |
          v                                 v                        v
 请求有限历史画面                      写入结果并独立读回        回调结果到 HA Monitor
 NyxID -> HA NyxID Node                     |
   -> Monitor -> Frigate                    | 核验及回调成功后
   <- 筛选后的有限图片页                     v
NyxID 云端                         可选的受限通知 service
                                   -> webhook 自动化，新线程
                                   -> 已有 NyxBot 独立查结果
                                   -> 固定的、用户授权的聊天
```

Monitor 用确定性规则和启发式方法筛选证据，视觉推理由 CMA Worker 完成。
默认 Frigate 预算最多取 24 个源帧、选 12 帧、合成 3 页；规则或测试可以进一步收紧。
原始录像留在 Frigate 配置的存储中。完成通知只携带结果引用，NyxBot 查已有结果生成总结。

餐厅示例在 Worker 独立读回十列表格、且 `recorded` 回调成功之后发通知。
接收器按稳定通知 ID 持久化去重，测试和 pending 记录不发业务总结；通知失败也不重新分析或写表。
这里的接收器配置在 NyxID webhook 自动化中。使用此方案不代表已经创建了独立 CMA 结果通道。
具体字段见[完成通知约定](../examples/dining/README.md#optional-completion-summaries)。

独立候选使用 profile target 和 `per_event` 分区。使用已有 Agent 作为 target 时，
即使设置此分区仍可能复用其原生上下文。当前一个 Monitor 实例共用一套 CMA 目的地址、
签名配置和 Worker 凭证；消费方支持相关事件类型和权限时，多条规则可以共用它。
新增规则不会自动创建 Worker，也不会自动修改其分析能力。

## 配置在哪里管理

| 配置 | 权威来源 | 生效方式 |
| --- | --- | --- |
| 摄像头、条件、时间窗口、证据预算、输出与结果约定 | Monitor 的持久化规则库 | HA 管理页面或 owner API 保存为新修订 |
| `pipeline_config` / `PIPELINE_CONFIG_*` | 首次启动的引导配置 | 只导入一次为 `default` v1，后续编辑规则库 |
| Frigate/CMA 地址、HMAC、凭证、清理范围和定时参数 | 私有 HA 插件选项 | 保存并重启 Monitor |
| MQTT/HA 订阅、转发路由和目的凭证 | 私有 Relay 选项 | 保存并重启 Relay |
| 本地目标和注入凭证 | NyxID service 记录与节点配置/本地凭证库 | 同步匹配的服务记录和节点配置 |
| 模型、提示词、权限、分区、通道输入 | CMA profile/channel 定义 | 使用管理 API 更新，并独立读回确认 |
| 总结提示词、机器人、固定目的聊天 | NyxID 通知自动化 | 配置并启用接收器，验证平台发送回执 |
| 录像保留期、硬盘/NAS | Frigate 与 HA 存储配置 | 在对应组件中应用 |

HA 规则页面目前只管理规则。还没有一条命令可以同步 HA、CMA 和 NyxID 的全部配置。
`examples/dining/render.py` 生成私有的云端应用、Worker、通道配置文件，
不会安装 HA 插件，也不会自动把文件发布到平台 API。

`examples/dining/worker-prompt.md` 是部署模板，部署时将文本写入 CMA profile。
修改并合并这个文件后，还需要应用远程 profile/channel 配置，已有 Worker 才会使用新内容。

## 哪些更新需要升级 HA 插件

| 改动 | 必须做什么 | 需要升级 HA 镜像吗 |
| --- | --- | --- |
| 规则、证据参数 | 保存规则修订，在安静时段确认判定 | 不需要 |
| Monitor 的连接、凭证、定时、清理设置 | 保存 HA 选项并重启 Monitor | 不需要 |
| Worker 提示词、模型、权限 | 应用 CMA 配置，检查拒绝项并独立读回 | 不需要；新通道地址、签名或清理标识仍需同步 HA 并重启 |
| 完成通知 | 配置接收器及 Worker hook/权限，验收一条历史补发并验证去重 | 使用现有餐厅 hook 无需改核心镜像 |
| 聚合、证据、API 或界面程序 | 发布核心镜像、更新 HA digest、发布插件新版本、升级已安装插件 | 需要 |
| HA 启动包装或选项 schema | 发布并升级 HA 插件新版本 | 需要 |
| Relay 或 NyxID Node 程序 | 发布并升级对应独立插件 | 只更新相应组件 |
| 文档 | 发布文档 | 不会改变已安装程序 |

核心程序的发布顺序：

```text
修改核心程序及版本 -> CI/测试 -> 发布不可变核心镜像 digest
  -> 更新 HA cma-event-pipeline/Dockerfile 的 pin、config.yaml 版本及 changelog
  -> HA 镜像构建和启动验收 -> 刷新商店并升级已安装插件
  -> 独立核对已安装版本、选项、健康和持久化
```

合并 PR 或成功构建镜像都不等于 HA 已升级。HA 插件固定核心 digest，发布核心
`main`/`latest` 不会自动改变已安装插件。现有 CI 触发条件可能在文档提交时也构建或发布镜像，
这仍不表示正在运行的实例已更新。插件版本与核心版本各自编号。
具体包装流程见[HA 插件说明](https://github.com/ChronoAIProject/nyx-homeassistant-node/tree/main/cma-event-pipeline#packaging-and-updates)。

## 更新 Worker 或通道的步骤

1. 私有保存当前通道定义、HA 选项、完整规则/数据备份和结果 schema，记录计划修改的提示词和权限。
2. 使用版本前提检查提交完整的 CMA profile/channel 配置，仅授予需要的服务；检查拒绝项并独立读回。
3. 若扩展权限返回 `binding_reconfiguration_unsupported`，新建具备完整最小权限的 profile/channel。
   新绑定可能创建新的专用 workspace，不能直接沿用旧清理标识。
4. 先运行有预算的验收任务。通知验收只读一条已核验记录，标明历史补发/通知测试，
   不重新看图或写表；再次提交相同通知 ID，确认没有第二次运行和群消息。
5. 切换 HA 前检查旧通道的发送队列和活跃工作，仍有旧请求时保留相关凭证。
   在 HA 中同步准确的事件地址、HMAC 及新 workspace/native profile 清理范围，重启 Monitor。
   保存可用的回滚记录。
6. 验证规则和历史保留、Worker 访问、结果独立读回、回调及通知发送回执。
   只清理确认专属于旧部署的资源，保留业务结果和稳定事件 ID。

规则与消费方约定分别更新：`output.event_type`、证据/结果路径、状态和字段必须对应。
多规则事件携带自己的版本化路径；旧的无版本接口仍绑定导入的 `default` v1。

## 数据与验收

| 数据 | 存放位置与生命周期 |
| --- | --- |
| 原始录像 | Frigate 配置的本地/NAS 存储及保留期 |
| Relay 队列 | Relay `/data`，与 Monitor 队列独立 |
| 规则、修订、判定、候选、回调、CMA 发送队列 | Monitor `/data/state`；备份完整 HA 数据 |
| 筛选后的图片页 | 有预算的临时缓存，所选证据会传给 CMA |
| 分析记录 | 配置的结果目的地，例如 Sheet |
| Worker 上下文和运行环境 | CMA；可选清理仅匹配专用 workspace/profile，保留业务结果 |
| 通知去重、运行记录、原生平台回执 | 接收器和平台；接收成功不等于发群成功 |

Relay 积压、Monitor 的 CMA 积压、Worker 清理计数要分别检查。
清理宽限期内的 `pending` 不表示候选事件未发送。CMA admission 不等于分析完成，
`recorded` 回调不等于已独立核验外部写入，通知排队不等于获得聊天平台发送回执。
界面行为变更还需在浏览器中验证中英文页面。离线重放、集成验证、真实活动验证各自有不同边界。

用一份私有安装清单记录实际 HA/核心版本和镜像 digest、规则修订、通道记录及版本、
提示词摘要、真实 workspace/profile、通知接收器、凭证引用与过期时间、验收和回滚记录。
这些是运维清单字段，不是新增的可执行配置 schema。凭证、摄像头/地址标识、目的地 ID、
证据和现场回执均保留在公开仓库之外。

操作细节见[规则管理](monitoring.md)、[HA 运维](home-assistant.md)、
[规则 schema](pipeline.md)与相应[场景示例](../README.md#examples)。
旧设计和 changelog 用于追溯历史，当前操作以现行 schema、脚本及已安装资源读回为准。
