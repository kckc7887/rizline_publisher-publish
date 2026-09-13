# 第三方代码与许可证

本文记录发布器使用的第三方代码、格式实现参考和直接 Python 依赖。各许可证仅适用于相应上游代码，不为整个发布器选择统一许可证。

## vgmstream 格式实现参考

`rizline_publisher/audio.py` 参照 vgmstream 对 CRI UTF、AFS2 和 HCA 头部的解析，使用 Python 实现仅供完整时长核验的元数据读取。它读取采样数、采样率、帧数、编码延迟和填充，并交叉检查边界；不包含 vgmstream 的播放、音频解码或解密实现。

来源为 [vgmstream/vgmstream](https://github.com/vgmstream/vgmstream)。以下链接固定于 2026-09-14 许可核验时的提交 `e6afeaacf433bfafd38d873f80c94517e09d5b96`，是本次核验快照，并非声称原实现曾锁定此版本：

- [src/util/cri_utf.c](https://github.com/vgmstream/vgmstream/blob/e6afeaacf433bfafd38d873f80c94517e09d5b96/src/util/cri_utf.c)：UTF 表头、列类型与存储方式，以及 HCA v3 单行表头超出声明行宽的兼容行为。
- [src/meta/awb.c](https://github.com/vgmstream/vgmstream/blob/e6afeaacf433bfafd38d873f80c94517e09d5b96/src/meta/awb.c)：AFS2 偏移表与对齐。
- [src/coding/libs/clhca.c](https://github.com/vgmstream/vgmstream/blob/e6afeaacf433bfafd38d873f80c94517e09d5b96/src/coding/libs/clhca.c)：HCA 头部字段、每帧采样数和编码填充。

vgmstream 使用其根目录 `COPYING` 中的 ISC 式许可。完整版权声明、各项 Portions 归属、授权条件和免责声明原样保存在 [LICENSES/vgmstream-COPYING.txt](LICENSES/vgmstream-COPYING.txt)，对应[上游原文](https://github.com/vgmstream/vgmstream/blob/e6afeaacf433bfafd38d873f80c94517e09d5b96/COPYING)。复制或分发上述参考实现的相关代码时，应同时保留该文件和本节归属说明。

此外保留 `clhca.c` 文件头列出的来源角色：nyaga 完成原始反编译及 C++ 解码器；kode54 移植为 C；bnnm 清理代码并再次分析 HCA v3，过程中参考 Thealexbarney 的 VGAudio 解码器；Youjose 提供 Ambisonics 信息。这些归属来自上游文件头，不表示发布器包含上述完整解码器。

本项目的实现范围与调整：用 Python `struct` 和整数读取元数据，以单个 waveform 的 ACB 为输入，增加采样数、帧长和文件完整性相互核验，拒绝需要额外 cue 映射的多 waveform 输入。

## 直接 Python 依赖

依赖由 `requirements.txt` 声明并通过 pip 安装；本仓库未复制其包源码。安装分发包附带的许可证和归属文件仍适用于对应包及其中的组件。这里列出直接依赖，不将各包的许可扩展到发布器自己的代码。

| 依赖约束 | 用途 | 包许可证 | 上游与许可 |
| --- | --- | --- | --- |
| `UnityPy==1.10.18` | 读取 Unity bundle、对象及纹理 | MIT | [K0lb3/UnityPy](https://github.com/K0lb3/UnityPy)，[LICENSE](https://github.com/K0lb3/UnityPy/blob/master/LICENSE)；安装的 1.10.18 分发包保留 K0lb3 的 2019–2021 版权声明 |
| `Pillow==11.3.0` | 将纹理图像编码为 PNG | MIT-CMU | [python-pillow/Pillow](https://github.com/python-pillow/Pillow)，[11.3.0 LICENSE](https://github.com/python-pillow/Pillow/blob/11.3.0/LICENSE) |
| `boto3>=1.35,<2` | S3 客户端与请求签名 | Apache-2.0 | [boto/boto3](https://github.com/boto/boto3)，[LICENSE](https://github.com/boto/boto3/blob/develop/LICENSE)、[NOTICE](https://github.com/boto/boto3/blob/develop/NOTICE)；实际安装版本由该范围解析，以对应分发包为准 |

## 调查与数据引用的边界

- [limmy114/rizline-tool 的固定提交](https://github.com/limmy114/rizline-tool/blob/a7e1ae23aaae215c36710899af363bc71ae32634/index.html)只用于 `upstream.py:parse_stats` 提取 `songAllData` 数据字面量；发布器不执行其 JavaScript，也未移植其前端或 RKS 计算代码。该提交仅包含 `index.html`，没有独立代码许可证或文件内授权声明；本文不为它指定许可证。
- [CHCAT1320/rizline-assets-get](https://github.com/CHCAT1320/rizline-assets-get)是前期资源结构调查来源，当前发布器没有捆绑或执行其脚本。该项目没有独立的项目代码许可证；其中附带的 vgmstream `COPYING` 只说明对应组件，不能视为 CHCAT1320 项目代码的许可证。

以上说明用于准确区分代码依赖、格式参考和数据读取，不把无许可的调查来源列为已获开源许可的代码。
