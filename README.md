# Rizline 个人曲库发布器

独立维护 rRanker 的 Rizline 曲库，从国服当前公开资源生成不可变版本，再发布到自己的 S3 兼容存储。手机端只读取自己的发布资源，不依赖 Wiki、GitHub 或游戏资源服务器。

本项目不登录游戏账号、不请求验证码、不导入玩家存档。

## GitHub Actions

仓库：<https://github.com/kckc7887/rizline_publisher-publish>。工作流分为两条：

- **校验发布器**：每次 push、Pull Request 或手动运行时，在 Ubuntu / Python 3.13 安装依赖，执行单元测试、语法与人工修订文件检查。不读取发布密钥。
- **构建与发布曲库**：手动运行，导入当前官方资源、校验、构建并保存可发布资源，再由单独任务下载并验证产物完整性。默认不写入 S3；勾选“实际上传至 S3 并切换 current”后，发布步骤才会使用密钥上传，且只允许从 `main` 发布。

### 配置 Secrets 与 Variables

进入仓库 **Settings → Secrets and variables → Actions**，分别在 Secrets 和 Variables 中创建以下仓库级配置：

| 类型 | 名称 | 填写内容 |
| --- | --- | --- |
| Secret，必填 | `AWS_ACCESS_KEY_ID` | 对象存储访问密钥 ID |
| Secret，必填 | `AWS_SECRET_ACCESS_KEY` | 与该 ID 配对的访问密钥 |
| Secret，仅临时凭据需要 | `AWS_SESSION_TOKEN` | 服务商签发的临时会话令牌；长期密钥不创建此项 |
| Variable，必填 | `RIZLINE_S3_ENDPOINT` | 服务商提供的 HTTPS S3 写入 endpoint |
| Variable，必填 | `RIZLINE_S3_REGION` | 服务商提供的签名 region |

固定目标桶为 `rranker-rizline-data`，无需另建桶名变量。endpoint 和 region 必须以存储控制台为准，公开读取域名不是写入 endpoint 的推导依据。密钥需有该桶 `rizline/` 对象的 `s3:GetObject`、`s3:PutObject`，以及桶级 `s3:ListBucket` 权限：按 [S3 GetObject 规则](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html)，缺少 ListBucket 时，不存在对象可能返回 403，发布器不能把它误认为可上传的新对象。工作流不修改桶配置或 ACL，也不删除旧版本。匿名读取在存储侧配置。

无需额外创建 GitHub PAT、`GITHUB_TOKEN` 或 GitHub Environment。未配置上述值时仍能运行校验和只构建模式；实际发布缺少配置会明确失败。

### 运行与下载

1. 在 **Actions → 构建与发布曲库 → Run workflow** 选择 `main`。首次可保持实际上传不勾选，生成资源检查报告。
2. `upload_workers` 默认 `4`，可选择 `1 / 4 / 8 / 12 / 16`。它控制封面与曲库的上传及远端 GET 校验并发；上游导入另固定为 4 并发。
3. 在运行摘要中查看版本和缺项统计，下载 `rizline-release-<运行ID>-<尝试次数>` 及 `rizline-reports-<运行ID>-<尝试次数>`。前者包含 `rizline/current.json` 和完整不可变版本，后者包含导入报告、资料补充表和上传计划；产物保留 90 天。原始音频、谱面和 HTTP 缓存不进入产物。
4. 完成配置并确认资料后，重新运行工作流并勾选实际上传。该次运行会重新检查上游当前版本；以该次构建摘要为准。所有内容上传及 GET 字节校验结束后才上传 manifest，manifest 验证后最后切换 current。

多次发布工作流通过同一 concurrency group 串行执行，不取消正在发布的运行；资源文件在每次运行内部并行处理。失败时不会继续切换指针，排队上传会取消，已开始的资源任务会收尾。不要与本地实际发布同时执行；本地 CLI 不参与 GitHub 的工作流锁。

发布任务失败后，修正配置可选择 **Re-run failed jobs**；发布任务按构建任务返回的产物 ID 下载，继续使用同一份已校验资源。重新运行整个工作流则会重新导入和构建。需要保留准确旧版本用于回滚时，请在产物到期前下载归档；解压 release ZIP 到本地 `dist/` 后可使用下文的校验与发布命令。

`overrides.json` 的人工资料提交到仓库后，下一次手动构建自动生效。没有定时发布或 push 后自动上传；所有 S3 写入均需手动选择实际上传。

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

必须自行配置服务商提供的写入 endpoint 和 region，不能从公开读取域名推断。认证使用 boto3 标准凭据链，支持环境变量、共享凭据文件或 AWS profile；项目不保存或打印密钥。

```powershell
$env:RIZLINE_S3_ENDPOINT = 'https://服务商提供的S3写入endpoint'
$env:RIZLINE_S3_REGION = '服务商提供的region'
$env:AWS_PROFILE = '已在本机配置的profile名称'
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher publish
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher publish --execute --workers 4
```

也可使用服务商提供的 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`，临时凭据额外需要 `AWS_SESSION_TOKEN`。请在本机自行设置，不写进项目文件。所需对象读写和桶级 ListBucket 权限见上文；工具不修改桶配置或对象 ACL。桶的匿名公开读取策略应由存储侧配置。

实际上传前校验本地版本全部 SHA-256 和文件大小；封面与曲库默认以 4 并发上传，`publish --workers N` 可设为 1–16。每个任务包含远端对象 GET，读取实际字节并重新计算 SHA-256 和大小；全部资源任务完成后再串行上传并核验 manifest，最后更新 `rizline/current.json`。current 上传后同样读取实际字节核验。对象自带的 `Metadata.sha256` 仅用于信息记录，不作为内容已验证的依据。

上传计划中的 `uploadOrder` 表示发布阶段顺序；资源文件在第一阶段内的实际完成顺序由并发任务决定，manifest 和 current 始终最后依次处理。

所有 PUT 都附带 `Content-MD5` 供 S3 校验传输。不可变对象还使用 `If-None-Match: *` 条件写入，防止查询之后发生并发覆盖；若并发写入返回 409/412，只在重新 GET 得到完全相同的内容时复用，否则中止，current 不切换。存储服务或本机 SDK 不支持这些标准条件时会报错，不自动降级为无条件覆盖。相关字段语义见 [S3 PutObject 文档](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html)。

已存在的不可变对象也必须逐个读取实际内容校验，因此重复发布会产生约一份完整发布资源大小的读取流量。不可变对象缓存一年，current 使用 `no-cache`。任何资源内容或大小不一致都会中止；失败时可重试。

默认不会删除旧版本；客户端仍可能持有旧版本中的封面链接。工具不修改 bucket policy、CORS、生命周期或公开权限。

### 回滚

保留 `dist/rizline/releases` 中的旧版本。先列出本地版本，再选择需要恢复的准确版本号：

```powershell
Get-ChildItem -LiteralPath '.\dist\rizline\releases' -Directory | Select-Object -ExpandProperty Name
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher rollback '需要恢复的准确resourceVersion'
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher validate --release
.\.venv\Scripts\python.exe -X utf8 -m rizline_publisher publish
```

`rollback` 先校验目标版本的所有资源，再只切换本地 current；资源缺失或摘要不一致时不切换。需要线上回滚时，在确认上传计划后执行 `publish --execute`。若旧版本只有远端副本，需要先将该版本 manifest 及其列出的所有对象完整恢复到相同本地路径；工具不会猜选旧版本，也不从远端自动恢复。

## 数据来源与口径

- 游戏版本来自[官方版本分发接口](https://rizserver.pigeongames.net/game/server_api/v1/dis)，国服标识 `pigeongames.rizline`、channel `11`。每次导入刷新该接口，再解析该版本 Android Addressables catalog 和增量资源链。
- 歌曲、谱面 ID、曲名、曲师、曲绘画师、定数、谱师、曲包分组和国服资源替换来自官方 `AssetList/Default`。普通谱与三个 `Disc O` SP 都按独立 level ID 保留；封面可以共享。曲包采用官方 `discName`（Disc 1、Disc 2、EX - T.S.、EX - Single、Disc O）。更细分系列可通过人工修订 `packId/packName` 维护。
- 难度等级取定数整数部分，移动版小数部分达到 `0.6` 时加 `+`；SP 使用官方特殊文本。规则见[机制说明](https://www.rizwiki.cn/index.php?title=%E6%9C%BA%E5%88%B6&variant=zh-sg)。SP 不参与 RKS，定数为空。
- BPM 是官方谱面 `bPM × bpmShifts.value` 的范围，消除 float32 误差后保留最多三位小数。它包含谱面中的速度变化，允许用核实的歌曲 BPM 进行人工修订。
- HIT 为所有 note 数量加 HOLD 数量，HOLD 头尾分别计入。COMBO 使用实际 HIT 分段倍率：前 5 HIT 各 1、接着 3 HIT 各 2、接着 3 HIT 各 3、其后各 4。
- Riztime HIT 初始补充源固定为 [limmy114/rizline-tool 的已审阅提交](https://github.com/limmy114/rizline-tool/blob/a7e1ae23aaae215c36710899af363bc71ae32634/index.html)。只解析其中 JSON 数据字面量，不执行网页 JavaScript。使用“曲名标准化或已审阅 ID 别名 + 官方 HIT 一致”双重匹配；Max Score 为 `1,000,000 + 100 × Riztime HIT`。官方定数始终优先。要使用新提交，可传 `import --stats-url 'https://raw.githubusercontent.com/.../提交SHA/index.html'`；`--stats-url ''` 可完全关闭补充源。不要直接依赖浮动分支作为正式发布来源。
- 完整时长来自官方 ACB 的 `WaveformTable.NumSamples / SamplingRate`，并与其内嵌完整 HCA 帧数、编码延迟及尾部填充交叉核验。不使用歌曲试听片段或谱面最后一个音符估算时长。格式事实参考 [CRI UTF](https://github.com/vgmstream/vgmstream/blob/master/src/util/cri_utf.c)、[AFS2](https://github.com/vgmstream/vgmstream/blob/master/src/meta/awb.c) 和 [HCA 元数据](https://github.com/vgmstream/vgmstream/blob/master/src/coding/libs/clhca.c)。仅读取格式元数据，无需解密或解码音频。
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

| 路径 | 职责 |
| --- | --- |
| `rizline_publisher/upstream.py` | 唯一 HTTP/cache 边界，Addressables、官方资源导入和经核验的统计补充 |
| `rizline_publisher/audio.py` | CRI UTF、AFS2、HCA 元数据时长核验 |
| `rizline_publisher/core.py` | 唯一合同校验、人工修订合并、确定性构建和发布事务 |
| `rizline_publisher/__main__.py` | CLI 编排与错误出口 |
| `overrides.json` | 应纳入版本管理的个人人工数据 |
| `.github/workflows/validate.yml` | push、Pull Request 与手动校验 |
| `.github/workflows/publish.yml` | 手动导入构建、产物归档、串行发布运行内的并行上传 |
| `.cache/` | 只保留本地的上游原始文件缓存 |
| `work/` | 只保留本地的导入结果、PNG、源表和审阅报告 |
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

测试覆盖：长键和多依赖 Addressables、SP 身份隔离、HIT统计与 BPM、统计源不匹配时保留空值、完整音频时长与编码填充、人工修订保留、确定性构建、路径越界、内容篡改、元数据正确但远端字节损坏、并发条件写入，以及上传/核验失败时不更新 current。S3 测试使用模拟客户端，不能替代真实存储发布验收。
