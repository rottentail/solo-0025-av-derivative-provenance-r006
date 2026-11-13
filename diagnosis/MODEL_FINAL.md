All evidence is in place and the code tree is untouched. Here is the complete diagnosis.

## 结论速览

两种损坏之所以结果不同，是因为它们在复验代码路径上**抛出了不同种类的异常，而验证器只捕获了其中一部分**。边界恰好落在 PCM 采样的**字节对齐**上，而不是“文件能不能被认出是 WAV”。

| 场景 | 源文件是否还能读 | 真实 HTTP 响应 | 审计记录（重开后仍在） | 含义 |
|---|---|---|---|---|
| A 源 blob 被删除 | 否（文件不存在） | `409 INTEGRITY_MISMATCH`，reason=`source_or_output_unavailable` | `mismatch`（终态） | 检查**跑完了**，结论是不通过 |
| B 截断奇数字节（少 1 字节） | 是，RIFF/WAVE 片头与 data 块头都完整 | `500 INTERNAL_ERROR` | `pending`（非终态，重试一次多一条） | 检查**根本没结束**，没有结论 |
| C 截断偶数字节（少 1 个采样） | 是，片头同样完整 | `409 INTEGRITY_MISMATCH`，reason=`reproduced_bytes_differ` | `mismatch`（终态，带复算摘要） | 检查跑完了，字节不一致 |

## 异常停在哪个边界（带真实回溯）

场景 B 的服务端日志里是未被捕获的原始异常：

```
File "/workspace/avprovenance/media/wav.py", line 103, in decode_wav
    interleaved.frombytes(raw)
ValueError: bytes length not a multiple of item size
```

调用链是 `http_api.do_POST → application.verify_rendition → verifier.reproduce_output → pipeline.apply_media_operations → audio_ops.render_audio → wav.decode_wav`。

关键机制有三层：

1. **片头检查不会发现尾部截断。** `inspect_media` 只看前 12 字节的 `RIFF…WAVE`（`media/inspector.py:14`），`wave.open` 对“头完整、data 负载短”的文件照常打开，`getnframes()` 仍按头部声明返回 **480** 帧；`readframes()` 不报错，只静默返回更少的字节（实测 1003 字节的文件只读出 **959** 字节 PCM）。
2. **边界在 `array.frombytes` 的 item 对齐。** `verifier.py:51` 的 `except` 只接住 `(MediaOperationFailure, UnsupportedMedia, RenderFailed)`。
   - 截断后 PCM 尾部是**奇数**字节 → `array('h').frombytes` 抛 **`ValueError`**，不在捕获名单里 → 一路冒泡到 `http_api.py:193` 的兜底 `except Exception` → 对外变成 `500 INTERNAL_ERROR`（不泄露路径）。
   - 尾部是**偶数**字节（整采样截断）→ `frombytes` 成功，复算正常完成，只是重建出的 WAV 与存档摘要不同 → 走 `reproduced_bytes_differ` 的正常 `mismatch` 分支（`verifier.py:64-72`）→ `409`。
   - （立体声下，偶数但非帧对齐会在 `wav.py:106-107` 被主动转成 `UnsupportedMedia`，那属于被捕获的 `mismatch`；唯独“item 大小都凑不齐”的 `ValueError` 漏网。）
3. **pending 在复算开始前就已落盘。** `application.py:277` 先 `_begin_integrity_audit` 写入一条 `pending`，只有走到 `settle_integrity_audit`（`application.py:334`）才会改成终态。`ValueError` 在这之前/期间逃逸，settle 永远不执行，于是记录停在 `pending`。

## 重开后的持久记录（真实 state.json）

审计是写进原子替换的 `state.json` 的，换一个全新进程对同一数据目录启动后依旧存在，且**重开不会自动补偿**：

- 场景 B 重开后 `integrity_checks` 里同时有：1 条基线 `verified` + **2 条 `pending`**，pending 记录 `expected_digest=null / reproduced_digest=null / reason=null`，且 `created_at == updated_at`（从未被 settle 触碰过）。
- 我在第二次重开后又对同一截断源发了一次复验：仍返回 `500`，审计计数 **3 → 4**，状态列表变成 `['pending','pending','pending','verified']`。即每次重试都新增一条永远无法终结的 pending，问题不会自愈，也不是清掉残留就能当它消失。
- 场景 A/C 重开后则都是干净的终态：A 为 `mismatch/source_or_output_unavailable`，C 为 `mismatch/reproduced_bytes_differ` 并保留了不同的复算摘要 `1f6c3cc0…` 对期望摘要 `6d3092e7…`。三个场景的 rendition 本身始终是 `completed`，未受影响。

## 对两种解释的影响

- **“检查没有通过”（场景 A、C）**：原成片仍可被读取并完成了一次完整复算，系统形成了最终结论。A 是“无法取得输入/输出”这一稳定 reason 的 mismatch；C 是“复算字节对不上”的 mismatch。两者都有终态审计、有 reason、有摘要，属于可审计的失败结论。
- **“检查根本没有结束”（场景 B）**：源文件物理上还在、WAV 片头也认得，但解码在 item 对齐处崩溃，复验在写入任何结论之前就被 500 打断。因此这条 `pending` **既不是 verified 也不是 mismatch**——它不代表“检查通过”，也不代表“检查判定不通过”，而是“本次复验没有产生任何最终判断”。把它数成失败会高估结论、数成通过更是错误；正确处理是把它识别为**悬挂/未完成**，并与终态 mismatch 分开统计。
- 需要区分的两个独立问题是：**(1) 原成片是否仍可读**（A 不可读 → 受控 mismatch；B/C 可读但内容已变），**(2) 本次复验是否形成了终态结论**（A/C 形成了；B 因未捕获的 `ValueError` 没形成）。B 的存在意味着“源还在、片头还认得出”并不能保证检查能结束。

## 复现方式与留存样本（未改任何代码）

- 代码树 `git status` 干净，`compileall` 与 `workflow_checks verify-reproduction` 均通过；只在隔离目录操作，未改 `avprovenance/` 任何文件，也未删除任何残留记录。
- 驱动与启动引导（独立于数据目录、用 `python3 -I` 运行）：`/tmp/av-diag/scripts/reproduce_diagnosis.py`、`/tmp/av-diag/scripts/launch_server.py`。
- 每个场景一个独立空数据目录，含真实媒体样本与重开日志，全部保留在 `/tmp/av-diag/runs/`：
  - `a-missing/`（删除源 blob）、`b-trunc-odd/`（1004→1003 字节，稳定触发 500 + pending，含 first/restarted/third 三份日志）、`c-trunc-aligned/`（1004→1002 字节，稳定触发 409 mismatch）；
  - 汇总证据 `runs/diagnosis-report.json`，各 `data/state.json` 即重开后仍持久的审计记录。
- 样本是确定性的单声道 s16le WAV（8000 Hz、480 帧），计划覆盖整片源（trim 0–0.06 s + gain −3 dB），保证截断尾部一定落在复算窗口内；少 1 字节与少 2 字节这一对样本即可稳定、可重复地复现“500+pending”与“409+mismatch”的差异。
