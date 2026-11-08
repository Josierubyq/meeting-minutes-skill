# 会议录音 → 会议记录

一个 [Claude Code](https://claude.com/claude-code) 的 skill:把会议录音整理成**中英对照**的会议记录,写清谁说了什么,提炼会议重点和下一步行动。可以出三种文件:

- **Word**(.docx)
- **PDF**
- **可点录音的网页版**(.html):点任何一句的时间,就从那一句开始播放录音。录音嵌在网页里,一个文件双击就能用。

中英夹杂的会议效果最好(中文句子里说的英文词会保持英文原样),全英文的会议也可以。

## 它是怎么工作的

这不是一个独立的软件,而是给 Claude Code 用的一套做法(`SKILL.md`)加几个脚本。你在 Claude Code 里把录音交给 Claude,Claude 按步骤做:

1. **转文字、分说话人**:在你自己的 Mac 上跑(Whisper large-v3-turbo、SenseVoice、sherpa-onnx)。**录音文件不上传。**
2. **定稿**:Claude 读转写稿,核对听错的词。拿不准的词会回到录音里重听那一段确认;确认不了的在文档里标一个小问号,听不出来的写「听不清」,不会编。
3. **认人**:根据内容判断每位说话人是谁,认不准时问你一次。
4. **翻译、写重点和下一步**,再生成你要的文件。

注意:第 2–4 步是 Claude 在对话里做的,所以**转写出来的文字会经过 Claude**。不想让会议内容离开电脑的,不适合用这个。

## 需要什么

| 要求 | 说明 |
|---|---|
| Mac,Apple 芯片(M1 或更新) | 转写用的 mlx-whisper 只能在 Apple 芯片上跑 |
| Claude Code | 桌面 App 或命令行都行,需要 Claude 的付费账号 |
| 约 3 GB 硬盘空间 | 模型约 2 GB |
| [uv](https://docs.astral.sh/uv/) | 用来装 Python 环境 |
| Microsoft Word(可选) | 只有出 PDF 时需要:PDF 由 Word 导出。没有 Word 也能出 Word 文件和网页版 |

## 安装

1. 装 uv(装过就跳过):

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. 把这个仓库放进 Claude Code 的 skill 文件夹。在本页面点绿色的 **Code** 按钮,复制仓库地址,然后:

   ```bash
   git clone <仓库地址> ~/.claude/skills/meeting-minutes
   ```

   不会用 git 也可以:点 **Code → Download ZIP**,解压后把文件夹改名为 `meeting-minutes`,放进 `~/.claude/skills/`(这是个隐藏文件夹,在 Finder 里按 `Cmd + Shift + .` 能看到)。

3. 装运行环境和模型(约 2 GB 下载,十几分钟):

   ```bash
   bash ~/.claude/skills/meeting-minutes/scripts/setup.sh
   ```

4. 重新打开 Claude Code。

## 怎么用

在 Claude Code 里把录音文件拖进对话框(或贴上文件路径),说一句「整理一下这次会议的录音」就行。支持 m4a、mp3、wav、mp4、mov 等。

顺便说这几件事,结果会准很多:

- **几个人发言**,各自是谁(比如「4 个人:王老师、Alex、小林,还有我」)。人多的会不说人数,说话人很容易分乱。
- 全程英文,还是中英混合。
- 会上常提到的项目名、人名、英文术语。

Claude 会问你要哪几种文件(Word / PDF / 网页版,可以多选)。一小时的录音,转文字约 17 分钟,整理约 10–20 分钟。

文件默认存到 `~/Documents/会议记录/`,文件名是 `年月日_会议主题_参会人`。想存到别处,直接跟 Claude 说。看完有要改的地方(人名、听错的词),跟 Claude 说,它会出一份新版本(`_v2`),旧版保留。

只要中文、不要英文的版本,也可以跟 Claude 说。

## 第一次用会遇到的弹窗

导出 PDF 时,macOS 可能问「是否允许控制 Microsoft Word」或「是否允许访问其他 App 的数据」,点**允许**。不点的话 PDF 会一直卡着不出来。

## 让转写更准:术语表

`glossary.txt` 里一行写一个词,转写时会提示给语音模型。把组里常说、容易被听错的英文术语、人名、项目名写进去。不要太多(只用前 40 个),词太多反而容易让模型把术语表原样吐出来。

## 文件说明

| 文件 | 作用 |
|---|---|
| `SKILL.md` | 给 Claude 看的完整步骤,整理质量主要靠它 |
| `scripts/setup.sh` | 装环境和模型 |
| `scripts/transcribe.py` | 转文字、分说话人;也负责重听拿不准的词 |
| `scripts/build_minutes.py` | 生成 Word / PDF / 网页版 |
| `scripts/build_player.py` | 生成可点录音的网页版(也可以事后单独补) |
| `references/minutes_format.md` | 整理结果的数据格式 |
| `glossary.txt` | 术语表 |

环境、模型和每次的中间文件放在 `~/.local/share/meeting-minutes/`。

## 已知局限

- 只在 macOS + Apple 芯片上用过。
- 离麦克风远、几个人同时说话时,转写和说话人区分都会出错。所以文档里会标出拿不准的地方,Claude 也会问你确认。
- 日语、韩语等其他语言没有专门处理。

---

## English

A Claude Code skill that turns a meeting recording into bilingual (Chinese–English) minutes: who said what, key points and action items. It can produce Word, PDF, and a single-file HTML page where clicking any timestamp plays the recording from that line.

Speech recognition and speaker diarization run locally on your Mac (Whisper large-v3-turbo, SenseVoice, sherpa-onnx), and the audio is never uploaded. The transcript text is then cleaned up, verified and translated by Claude in your Claude Code session, so the text does go through Claude.

**Requirements:** an Apple-silicon Mac, Claude Code with a paid Claude plan, about 3 GB of disk space, and [uv](https://docs.astral.sh/uv/). Microsoft Word is needed only for PDF export.

**Install:**

```bash
git clone <this repo URL> ~/.claude/skills/meeting-minutes
bash ~/.claude/skills/meeting-minutes/scripts/setup.sh
```

Then restart Claude Code.

**Use:** drop a recording into Claude Code and ask it to write up the meeting. Mention how many people spoke and who they are. The instructions in `SKILL.md` are written in Chinese.
