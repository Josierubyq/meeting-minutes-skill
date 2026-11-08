# minutes.json 与译文文件格式

`build_minutes.py`(和 `build_player.py`)读取工作目录里的三样东西:`transcript.json`(转写脚本生成,不要手改)、`minutes.json`、`translations/*.json`(可选)。

## minutes.json

```json
{
  "date": "2026-11-04",
  "time": "15:30–16:20",
  "topic": "数据清洗方案进展讨论",
  "topic_en": "Progress on the Data-Cleaning Plan",
  "participants": ["王老师", "Alex", "小林"],
  "me": "小林",
  "place": "线上 Zoom",
  "speakers": {"S1": "小林", "S2": "王老师", "S3": "Alex"},

  "key_points": [
    {
      "title": "评测进展",
      "zh": "baseline 的 accuracy 为 58.4%，换成新的清洗流程后提升到 76%。",
      "en": "Baseline accuracy is 58.4%; the new cleaning pipeline raises it to 76%.",
      "refs": [1]
    }
  ],

  "next_steps": [
    {
      "owner": "小林",
      "zh": "跑 ablation，把结果发给王老师和 Alex。",
      "en": "Run the ablation and send the results to the professor and Alex.",
      "due": "11/13（周五）前",
      "refs": [3, 4]
    }
  ],

  "utterances": {}
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `date` | 是 | `YYYY-MM-DD`,用于文件名和标题区 |
| `topic` | 是 | 中文主题,用于文件名和标题 |
| `participants` | 是 | 参会人,用于文件名和标题区,顺序即显示顺序 |
| `time` | 否 | 会议起止时间,显示在日期后面 |
| `topic_en` | 否 | 英文主题,显示在标题下 |
| `me` | 否 | 用户本人在会议里的名字;用户自己的下一步事项排在最前并加浅底色 |
| `place` | 否 | 地点或会议软件 |
| `speakers` | 是 | 转写稿里的 S 编号 → 名字;没写的显示为"说话人 N" |
| `key_points[].refs` / `next_steps[].refs` | 否 | 依据的段落编号,文档里显示为时间点 |
| `next_steps[].due` | 否 | 期限,没有就写"未定" |
| `utterances` | 否 | 逐段内容,格式同下面的译文文件;段落多时放到 `translations/` 里 |

## 译文文件 `translations/partNN.json`

键是段落编号(字符串),值是这段的修改和译文。多个文件会按文件名顺序合并。

```json
{
  "1": {"fix": "老师好，我先汇报一下这周的进展。我们先跑了 baseline……", "en": "Hi Professor, let me first report……"},
  "3": {"zh": "我同意。你能不能也跑一下 ablation……"},
  "4": {"fix": "好的，没问题。I will run the ablation and send you the results by Friday.", "orig": "zh", "en": "Sure, no problem. I will run……"},
  "9": {"drop": true},
  "12": {"speaker": "S2", "en": "……"}
}
```

| 键 | 用途 |
|---|---|
| `en` | 中文原话的英文译文 |
| `zh` | 英文原话的中文译文 |
| `fix` | 定稿后的原话(改正听错的词、去掉语气词)。不写就用转写结果 |
| `orig` | 原话放在哪一栏:`zh` 或 `en`。默认按原话里汉字的比例自动判断;中英各半的句子可以手动指定 |
| `drop` | `true` 表示这段不进文档(噪音、幻听、无关闲聊) |
| `speaker` | 改说话人归属,填 S 编号 |

每个没被 `drop` 的段落都要有译文,否则脚本会报错并列出缺的编号。只想先看排版时可以加 `--draft`。

## 文字里的两个标记

任何文字(原话、译文、会议重点、下一步、期限)里都可以用:

| 写法 | 意思 | 文档里显示 |
|---|---|---|
| `词[?]` | 有道理但没被录音确认 | 词后面一个橙色小问号 |
| `[听不清]` / `[inaudible]` | 录音里听不出来 | 浅灰色小字 |

出现标记时,发言记录上方的说明会自动加一句解释这两个标记。

## 文档里的样子

- 发言记录是三栏表格:发言人(名字 + 时间点) | 中文 | 英文。原话黑字,译文灰字;原话里夹带的另一种语言的词用蓝色标出,一眼能看出哪些词是用英文说的。
- 会议重点是编号列表,每条中文在上、英文在下,后面跟着依据的时间点。
- 下一步是表格:序号 | 负责人 | 事项(中英) | 期限 | 出处时间点。
- 页眉是主题和日期,页脚是"第 X 页 / 共 Y 页"。字体:中文等线,英文 Calibri。
- 表格每一行不会被拆到两页;表头在每页重复。
