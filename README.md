# Rizline 个人曲库发布器

独立维护 rRanker 的 Rizline 曲库，从国服当前公开资源生成不可变版本，再发布到自己的 S3 兼容存储。手机端只读取自己的发布资源，不依赖 Wiki、GitHub 或游戏资源服务器。

本项目不登录游戏账号、不请求验证码、不导入玩家存档。

第三方代码来源、直接 Python 依赖的许可证及引用边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。音频格式参考代码的完整上游许可保存在 [LICENSES/vgmstream-COPYING.txt](LICENSES/vgmstream-COPYING.txt)，各许可仅适用于对应第三方代码。

## GitHub Actions

仓库：<https://github.com/kckc7887/rizline_publisher-publish>。工作流分为两条：

- **校验发布器**：每次 push、Pull Request 或手动运行时，在 Ubuntu / Python 3.13 安装依赖，执行单元测试、语法与人工修订文件检查。不读取发布密钥。
- **构建与发布曲库**：每天北京时间 **20:00** 从 `main` 自动导入、校验、构建并实际发布；也支持手动运行。手动默认只构建，勾选“实际上传至 S3 并切换 current”才上传。两种方式共用相同的发布事务，实际发布只允许从 `main` 执行。

定时表达式为 UTC `0 12 * * *`，每天一次。GitHub 调度在繁忙时可能延迟，公共仓库连续 60 天没有仓库活动时可能停用定时工作流；这些是 [GitHub 调度限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)，不是严格准点执行保证。

### 只需配置两个 KEY

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，只创建以下两项：

| 类型 | 名称 | 填写内容 |
| --- | --- | --- |
| Secret，必填 | `AWS_ACCESS_KEY_ID` | 对象存储访问密钥 ID |
| Secret，必填 | `AWS_SECRET_ACCESS_KEY` | 与该 ID 配对的访问密钥 |

API 端点、桶名和签名参数均已内置，不需要创建 Variables 或其它 Secret。两个 KEY 都填写雨云对象存储提供的对应值。

没有 KEY 时仍可运行校验和只构建模式；实际上传只检查这两个 KEY 是否已配置。

### 运行与下载

1. 在 **Actions → 构建与发布曲库 → Run workflow** 选择 `main`。首次可保持实际上传不勾选，生成资源检查报告。
2. `upload_workers` 默认 `4`，可选择 `1 / 4 / 8 / 12 / 16`；`parse_workers` 默认 `4`，可选择 `1 / 2 / 4 / 8`。定时运行两者均为 `4`。解析、构建与本地校验复用有界工作池；每次实际上传及远端 GET 校验也并行执行，排队任务最多为工作线程数的两倍。
3. `rizline-release-<运行ID>-<尝试次数>` 是确定性构建包，`rizline-reports-<运行ID>-<尝试次数>` 是导入报告。实际发布另存 `rizline-publication-<运行ID>-<尝试次数>`，包含真正选中的发布版本；`rizline-publication-report-<运行ID>-<尝试次数>` 包含成功或失败阶段、指针结果及精确清理重试记录。失败候选包不代表已上线，以报告为准。产物均保留 90 天，不包含原始音频、谱面或 HTTP 缓存。
4. 两个 KEY 配置完成后，每天会自动实际发布；需要立即运行时，勾选实际上传。已有 `manifest.json` 就是唯一资源清单：游戏版本和文件身份（相对路径、大小、SHA-256）一致时跳过上传；对不上时按北京日期开新目录，未变封面 CopyObject，只 PUT 新增和变更，再条件切换 current。切换成功后删除 `rizline/releases/` 下不属于新 current 的对象。

多次发布工作流通过同一 concurrency group 串行执行，不取消正在发布的运行；资源文件在每次运行内部并行处理。上传失败时取消排队任务并等待在途任务结束，后续 manifest/current 阶段不执行。本地 CLI 复用相同事务；独立发布者同时运行时，current 的 ETag 条件写入阻止旧运行覆盖新指针。

首次导入需要读取完整上游资源，耗时受官方服务器网络影响；首次 Ubuntu 实测约 44 分钟。构建和发布任务各设为 90 分钟上限，并缓存 `.cache/http` 供后续运行复用；该缓存不进入发布资源包。网络重试和并发均有上限。

发布任务失败后，修正配置可选择 **Re-run failed jobs**；发布任务按构建任务返回的产物 ID 下载，继续使用同一份已校验资源。重新运行整个工作流则会重新导入和构建。需要保留准确旧版本用于回滚时，请在产物到期前下载归档；解压 release ZIP 到本地 `dist/` 后可使用下文的校验与发布命令。

`overrides.json` 的人工资料提交到仓库后，下一次定时或手动构建生效。push 本身只触发校验，不立即上传。

## 本地使用

需要 Python 3.10+；当前实测 Python 3.13、UnityPy 1.10.18、PowerShell 7。Windows 自动使用 PowerShell 的系统网络链路；其它平台使用 Python urllib。所有命令在本项目目录执行。

```powershell
Set-Location 'D:\Projects\rizline-resource-publisher'
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher import
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher validate
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher build
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher validate --release
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher publish
```

`publish` 默认只展示上传计划。`import` 初次需要下载当前版本的索引资源、全部谱面、封面和完整音频；后续复用按 URL 存储的本地缓存。音频只用来读取完整时长，不进入发布目录。导入默认 4 个并发，可用 `import --workers 2` 降低并发。中途失败可直接重试，旧曲库不会被部分结果替换。

已有依赖时可直接将上面的 `.\.venv\Scripts\python.exe` 换成 `py`。

## 日常维护

游戏更新后依次执行 `import`、`validate`、`build`、`publish`。查看 `work/import-report.json` 的未匹配统计与缺失字段，再决定实际发布。导入失败明细写入 `work/import-failures.json`。

`import` 和 `build` 会生成 `work/supplement-template.json`，结构与 `overrides.json` 相同。它列出每项缺失字段对应的完整官方歌曲/谱面 ID；初始内容包括 148 个 `updatedAt` 日期槽位和三个 SP 的 `maxScore/riztimeHit` 槽位。已生效的人工修订不再列入补充表，SP 有意为空的 RKS 定数也不列为缺项。

核实数据后，只把填好的字段及对应 ID 复制到 `overrides.json`。**不要把整份补充模板合并或覆盖过去**：模板中的 `null` 只是待核实占位，永久写入人工修订会覆盖将来上游已经补齐的值。模板只是一份可重新生成的工作表，导入/构建不会改写人工修订文件。统计源的歧义或 HIT 不一致仍保留在 `import-report.json`，包括候选源值、官方 HIT 和未采用原因，不能靠复制空值消除冲突。

`overrides.json` 是人工修订的唯一入口，导入和构建都不会改写它。按实际 ID 填写，不按曲名关联：

```json
{
  "schemaVersion": 1,
  "songs": {
    "PastelLines.RekuMochizuki.0": {
      "updatedAt": "2026-09-11",
      "durationSeconds": 110.387664
    }
  },
  "charts": {
    "chart.PastelLines.RekuMochizuki.0.IN": {
      "designer": "经核实的谱师署名"
    }
  },
  "statAliases": {},
  "achievementSongs": {}
}
```

以上日期仅用于说明格式，不代表该歌曲真实更新时间。修改现有文件时保留其中已经审阅过的 `statAliases` 和 `achievementSongs`，不要用示例整体覆盖。

- `songs` 支持歌曲元数据及完整 `achievements` 数组修订，禁止更改身份、封面路径或直接改写谱面列表。
- `charts` 支持定数、等级、谱师及物量修订，禁止改写 ID、所属歌曲或难度类型。HIT/COMBO、Max Score/Riztime HIT 必须自洽；SP 定数始终为 `null`。
- `statAliases` 为“官方歌曲 ID → 统计源曲名”。只在逐谱面 HIT 完全相等时使用统计源的 Riztime HIT；没有匹配或物量变动时留空，不外推。
- `achievementSongs` 为“官方成就本地化 ID → 官方歌曲 ID 数组”。当前源的成就 ID 带方括号，例如 `[dot_line]`，保留实际标识。空数组表示不关联任何歌曲。歌曲 `achievements` 中仅有 `id/title/condition`，不涉及玩家解锁状态。

手动修改歌曲/谱面元数据后，运行 `validate`、`build` 即可生效；修改导入映射 `statAliases`、`achievementSongs` 后需要重新 `import`。未知歌曲/谱面修订 ID 会报错，避免删除或改名后人工修订悄悄失效。

## 实际发布

固定目标桶是 `rranker-rizline-data`，固定公共读取根地址是：

`https://rranker-rizline-data.cn-nb1.rains3.com`

本地 CLI 与工作流共用 `core.publish` 的内置目标：API 端点 `https://cn-nb1.rains3.com`，桶名 `rranker-rizline-data`，签名区域 `us-east-1`。该桶的实际 GetBucketLocation 响应为零长度 LocationConstraint，按 [S3 协议定义](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetBucketLocation.html) 对应 `us-east-1`；用户无需填写这些参数。认证使用 boto3 标准凭据链，项目不保存或打印密钥。

```powershell
$env:AWS_ACCESS_KEY_ID = '你的 Access Key'
$env:AWS_SECRET_ACCESS_KEY = '你的 Secret Key'
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher publish
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher publish --execute --workers 4
```

已有本机 AWS profile 时也可使用 `AWS_PROFILE`。不要把实际 KEY 写进项目文件。密钥需有目标桶的对象读取、写入、复制、删除及桶级 ListBucket 权限；工具不修改桶配置或对象 ACL。桶的匿名公开读取策略应由存储侧配置。

实际上传前并行校验本地产物的 SHA-256 与大小。发布器读取 S3 的 current、manifest 和 catalog，先检查每一级实际字节的哈希与合同。对照只看游戏版本和每个资源的相对路径、大小、SHA-256；不比较 `generatedAt` / `publishedAt` / 随机修订号。曲库业务字段必须一致。完全一致时不执行任何资源 PUT 或 CopyObject，也不 GET 封面来重复算哈希，不改 current，不做清理。manifest 已满足资源清单需求，不生成第二份清单。

任意资源不同，或远端 manifest/catalog 缺失、损坏时，分配北京日期目录 `rizline/releases/YYYY-MM-DD/`（当天已是线上目录则用 `-2`、`-3`）。未变封面在桶内 CopyObject 到新目录，新增和内容变化的文件从 GitHub 工作副本 PUT，清单里已经没有的文件不复制。目标日期前缀若存在但不是 current（失败残留）会先清空再复用。权限拒绝和网络错误会失败，不当成“资源不存在”。禁止直接改正在使用的线上目录。差量发布前先在 `rizline/publisher-checks/` 用小对象验证条件写入，再用一对探针键验证同桶 CopyObject；检查或探针清理失败时不写任何正式发布对象。清单一致时不运行探针。`build` 的确定性结果和原始 `dist/` 不改变，实际候选版本单独写入 `work/publication-release/`。相同清单时也会用已验证的远端元数据和相同的本地资源重建准确归档，不下载封面。

发布阶段顺序固定为：并行 CopyObject 未变文件并 Head 核对大小、PUT 新增和变更并分别 GET 验证实际字节 → 上传并验证 manifest → 条件写入 current 并 GET 验证 → 清理 `rizline/releases/` 下不属于新 current 的对象。资源上传、解析、哈希和校验的独立任务并行执行；版本发现的前置依赖与发布阶段屏障保持顺序。单文件 PUT 对超时和断线做有限次重试。所有 PUT 附带 `Content-MD5`；资源和 manifest 使用 `If-None-Match: *`，current 使用起始读取的 ETag 做 `If-Match`，首次发布则使用 `If-None-Match: *`。409/412 冲突中止，不降级为无条件覆盖。不可变对象缓存一年，current 使用 `no-cache`。存储端必须实际支持这些标准条件和 CopyObject，参见 [S3 PutObject 文档](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html)。

玩家在漫长上传期间继续读取完整旧版，指针切换之前不删旧资源。切换成功后立即清理上传前记录的 **releases 残留**（含旧 live 和失败留下的目录）；其它前缀如 `other/` 不受影响。每批最多删除 1,000 个对象，删除前及最终完成前重查 current，并校验对象实际消失。批量删除在签名之前对 SDK 序列化的 XML 计算 `Content-MD5`。本事务不修改桶配置、ACL、版本控制或生命周期。

采用立即清理策略后，仍持有旧曲库且未缓存封面的客户端可能需要刷新曲库；已经缓存的资源可继续离线使用。上传失败不切换；current 写入或回读失败时不开始清理，但网络异常可能使指针结果不确定，报告中 `currentSwitched: null` 表示未能确认。清理失败时新版仍可能已正常上线；重试只处理已记录的旧键，不重新上传。

`work/publication-report.json` 在实际发布成功或失败时写入阶段、计数、准确版本和未完成删除键。每次差量发布的清理记录写入独立的 `work/cleanup-receipts/<实际发布版本>.json`，在 current 切换前落盘，不覆盖以前的记录。可先查看记录的清理计划，再显式执行：

```powershell
py -X utf8 -m rizline_publisher cleanup --receipt 'work/cleanup-receipts/实际发布版本.json'
py -X utf8 -m rizline_publisher cleanup --receipt 'work/cleanup-receipts/实际发布版本.json' --execute
```

重试会验证原桶、端点、区域、目标 current 及 ETag，只删除原快照中的剩余键；current 已变化时拒绝继续清理，不能扩大删除范围。下载 Actions 报告中的清理记录也可用此命令重试。`publish --report`、`--publication-output`、`--cleanup-receipt` 可自定义报告、归档和记录路径；指定的记录文件若已存在会拒绝覆盖。

### 回滚

保留 `dist/rizline/releases` 中的旧版本。先列出本地版本，再选择需要恢复的准确版本号：

```powershell
Get-ChildItem -LiteralPath '.\dist\rizline\releases' -Directory | Select-Object -ExpandProperty Name
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher rollback '需要恢复的准确resourceVersion'
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher validate --release
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher publish
```

`rollback` 先校验目标版本的所有资源，再只切换本地 current；资源缺失或摘要不一致时不切换。需要线上回滚时，使用本地保留版本或下载的 Actions 完整资源包，确认上传计划后执行 `publish --execute`。所选内容会按文件身份比较，必要时按北京日期开新目录做差量发布，并清理此前 releases 残留。工具不会猜选旧版本或从远端自动恢复已删除资源。

## 数据来源与口径

- 游戏版本来自[官方版本分发接口](https://rizserver.pigeongames.net/game/server_api/v1/dis)，国服标识 `pigeongames.rizline`、channel `11`。每次导入刷新该接口，再解析该版本 Android Addressables catalog 和增量资源链。
- 歌曲、谱面 ID、曲名、曲师、曲绘画师、定数、谱师、曲包分组和国服资源替换来自官方 `AssetList/Default`。普通谱与三个 `Disc O` SP 都按独立 level ID 保留；封面可以共享。曲包采用官方 `discName`（Disc 1、Disc 2、EX - T.S.、EX - Single、Disc O）。更细分系列可通过人工修订 `packId/packName` 维护。
- 难度等级取定数整数部分，移动版小数部分达到 `0.6` 时加 `+`；SP 使用官方特殊文本。规则见[机制说明](https://www.rizwiki.cn/index.php?title=%E6%9C%BA%E5%88%B6&variant=zh-sg)。SP 不参与 RKS，定数为空。
- BPM 是官方谱面 `bPM × bpmShifts.value` 的范围，消除 float32 误差后保留最多三位小数。它包含谱面中的速度变化，允许用核实的歌曲 BPM 进行人工修订。
- HIT 为所有 note 数量加 HOLD 数量，HOLD 头尾分别计入。COMBO 使用实际 HIT 分段倍率：前 5 HIT 各 1、接着 3 HIT 各 2、接着 3 HIT 各 3、其后各 4。
- Riztime HIT 初始补充源固定为 [limmy114/rizline-tool 的已审阅提交](https://github.com/limmy114/rizline-tool/blob/a7e1ae23aaae215c36710899af363bc71ae32634/index.html)。只解析其中 JSON 数据字面量，不执行网页 JavaScript。使用“曲名标准化或已审阅 ID 别名 + 官方 HIT 一致”双重匹配；Max Score 为 `1,000,000 + 100 × Riztime HIT`。官方定数始终优先。要使用新提交，可传 `import --stats-url 'https://raw.githubusercontent.com/.../提交SHA/index.html'`；`--stats-url ''` 可完全关闭补充源。不要直接依赖浮动分支作为正式发布来源。
- 完整时长来自官方 ACB 的 `WaveformTable.NumSamples / SamplingRate`，并与其内嵌完整 HCA 帧数、编码延迟及尾部填充交叉核验。不使用歌曲试听片段或谱面最后一个音符估算时长。格式实现参考 [CRI UTF](https://github.com/vgmstream/vgmstream/blob/e6afeaacf433bfafd38d873f80c94517e09d5b96/src/util/cri_utf.c)、[AFS2](https://github.com/vgmstream/vgmstream/blob/e6afeaacf433bfafd38d873f80c94517e09d5b96/src/meta/awb.c) 和 [HCA 元数据](https://github.com/vgmstream/vgmstream/blob/e6afeaacf433bfafd38d873f80c94517e09d5b96/src/coding/libs/clhca.c)，来源角色、核验快照与许可见 [第三方代码声明](THIRD_PARTY_NOTICES.md#vgmstream-格式实现参考)。仅读取格式元数据，无需解密或解码音频。
- 相关成就的名称和条件来自官方简体中文本地化，去除显示用富文本标签。只关联条件中明确涉及的歌曲；通用成就和整个 Disc 的完成成就不散发到每首歌。普通歌曲成就不会因为同名自动附加到 SP。
- `updatedAt` 专指游戏中歌曲/谱面的最近一次更新日期，格式 `YYYY-MM-DD`。官方资源表没有逐曲日期，所以初始值为空，待结合官方公告/Wiki 真实更新事件人工填写。HTTP Last-Modified、Wiki 编辑时间、导入时间均不替代这个字段。
- 游戏美术、音乐和相关署名归原权利人所有；本项目只发布曲库元数据与展示封面，音频和谱面原文件不随发布产物上传。

## 输出合同与项目结构

所有远端路径相对于公共读取根地址，不带前导 `/`：

```text
dist/
  rizline/current.json
  rizline/releases/<resourceVersion>/manifest.json
  rizline/releases/<resourceVersion>/catalog.json
  rizline/releases/<resourceVersion>/covers/<sha256>.png
```

`current.json` 包含 `schemaVersion/resourceVersion/manifestPath/manifestSha256`；manifest 包含 `schemaVersion/resourceVersion/gameVersion/files/catalogPath`，每个文件含 `path/size/sha256`。catalog 包含 `schemaVersion/resourceVersion/gameVersion/songs`，其中封面是当前不可变版本内的相对路径。

构建版本由官方资源版本与最终元数据/封面摘要共同决定。同样的输入产生同样的版本；任何人工修订或封面内容变化都会产生新版本。先写完整版本，再原子替换本地 current。

这里的相同输入指最终元数据与 PNG 文件字节相同。不同平台的 PNG 编码字节可能不同，即使封面像素一致，跨平台导入也可能生成不同的资源版本号。需要重发同一准确版本时，直接复用已归档的发布产物。

| 路径 | 职责 |
| --- | --- |
| `rizline_publisher/upstream.py` | 唯一 HTTP/cache 边界，Addressables、官方资源导入和经核验的统计补充 |
| `rizline_publisher/audio.py` | CRI UTF、AFS2、HCA 元数据时长核验 |
| `rizline_publisher/core.py` | 唯一合同校验、人工修订合并、确定性构建、有界并发与兼容发布入口 |
| `rizline_publisher/publication.py` | 文件身份比较、日期目录差量归档、S3 条件发布与残留前缀清理重试 |
| `rizline_publisher/storage_check.py` | 独立临时键上的 S3 条件写入和 CopyObject 能力核验 |
| `rizline_publisher/__main__.py` | CLI 编排与错误出口 |
| `THIRD_PARTY_NOTICES.md`、`LICENSES/` | 第三方代码来源、依赖许可证和格式参考代码的完整许可 |
| `overrides.json` | 应纳入版本管理的个人人工数据 |
| `.github/workflows/validate.yml` | push、Pull Request 与手动校验 |
| `.github/workflows/publish.yml` | 每日北京时间 20:00 自动发布、手动构建/发布、实际版本归档、差量上传和残留目录清理 |
| `.cache/` | 只保留本地的上游原始文件缓存 |
| `work/` | 只保留本地的导入结果、发布归档、报告与独立清理重试记录 |
| `dist/` | 只保留本地的待发布资源 |
| `tests/` | 不依赖网络的解析、校验、事务和数据边界测试 |

本项目独立于 rRanker 的 Git 仓库，CLI 不改动 rRanker 源文件。`.cache/`、`work/`、`dist/`、虚拟环境和凭据文件已排除在 Git 之外；构建资源通过 Actions 产物与 S3 交付。

### 初始版本覆盖

当前初始输入为官方 `2.7.1 / v141_2_7_1_3c13bbff2bP`：148 个独立歌曲条目、438 张谱面、145 张不同封面。全部歌曲已有完整音频时长、BPM、曲师和画师；全部谱面已有谱师、HIT 和 COMBO。435 张普通谱面有 Max Score 和 Riztime HIT，3 张 SP 的这两个字段为空。相关成就共 28 处歌曲关联。

148 个游戏更新时间仍为空，等待真实游戏更新事件的人工核实。初始发布资源共 146 个内容文件（曲库加封面），约 32.2 MB，不包含原始谱面和音频。具体版本、计数、大小以 `validate --release` 的实际输出为准。

## 验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher validate --release
```

测试覆盖：长键和多依赖 Addressables、SP 身份隔离、HIT统计与 BPM、统计源不匹配时保留空值、完整音频时长与编码填充、人工修订保留、确定性构建、路径越界、内容篡改、文件身份一致时零资源 GET/PUT、单文件变更只 PUT 该文件并 CopyObject 其余、日期目录与 `-2` 碰撞、失败残留复用、禁止写 live 前缀、缺失/损坏清单隔离恢复、并发资源屏障、current 条件竞争、切换后只留新前缀、分页与部分删除重试，以及每日调度和手动运行条件。S3 单元测试使用模拟客户端；真实存储的条件写入、CopyObject、公共读取与上传期间客户端体验仍需存储侧验收，单元测试不替代云端验收。
