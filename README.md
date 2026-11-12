# Tender Watch

招投标文件变更追踪与证据归档的可运行最小实现。

## 责任边界

- `fetching.py`：负责 URL 或 fixture 内容获取。
- `extract.py`：负责 HTML 元数据与附件链接解析。
- `dedup.py`：负责文件指纹和完整证据快照指纹。
- `versioning.py`：负责判定一次采集是否构成有意义的修订及其原因。
- `storage.py`：负责 SQLite 表结构、幂等写入、旧库升级和时间线查询。
- `pipeline.py`：只负责编排上述职责。

## 采集与时间线

```bash
# 原有单条采集方式保持不变；重复采集返回 changed=False，不产生伪版本
PYTHONPATH=src python3 -m tender_watch.cli \
  --fixture fixtures/sample_notice.html \
  --database /tmp/tender-watch.sqlite3

# 审核人员查看某个项目编号的完整证据时间线
PYTHONPATH=src python3 -m tender_watch.cli \
  --database /tmp/tender-watch.sqlite3 \
  --timeline TENDER-2026-001

# 人类可读格式
PYTHONPATH=src python3 -m tender_watch.cli \
  --database /tmp/tender-watch.sqlite3 \
  --timeline TENDER-2026-001 --format text
```

`--timeline` 默认输出 JSON，包含：

- `current`：当前公告及其对应版本号；
- `versions`：每个不可变版本的可复原公告字段、附件集合、附件是否可用、
  内容指纹、修订原因和差异明细；
- `collections`：每一次采集尝试（含无变化重复和附件失败）及其关联版本、
  每个附件的本次观测状态与错误信息；
- `integrity`：当前公告、版本历史、附件证据三者的一致性检查结果。

## 版本判定规则

一次采集只有产生“有意义的新证据”时才追加版本；重复提交绝不制造伪版本。

| 场景 | 是否新版本 | 原因标识 |
|---|---|---|
| 公告正文或标题/发布日期/预算/截止时间变化 | 是 | `body_changed`、`title_changed`、`published_at_changed`、`budget_changed`、`deadline_changed` |
| 仅来源地址变化（正文相同） | 是 | `source_url_changed` |
| 附件链接新增 | 是 | `attachment_added` |
| 附件链接从公告中消失（不是下载失败） | 是 | `attachment_removed` |
| 同一附件链接标题变化 | 是 | `attachment_title_changed` |
| 同一附件内容替换 | 是 | `attachment_content_changed` |
| 曾有版本中该附件不可用，本次补采成功 | 是 | `attachment_backfilled` |
| 已确认可用的附件本次暂时取不到 | 否，仅记录采集观测 | `fetch_failed` |
| 一直不可用的附件再次取不到 | 否，仅记录采集观测 | `still_unavailable` |
| 内容完全相同的重复采集 | 否 | `no change` |

关键点：

- 附件下载失败不会被当作“附件被删除”。只有解析出的公告链接集合发生
  变化才会产生 `attachment_removed`。
- 已确认的附件字节按内容哈希存入不可变 blob 表，后续失败不会清空旧内容；
  失败只会被记录为一次采集观测。
- “先不可用、后补采成功”若发生在最新版本之后，会产生
  `attachment_backfilled` 版本；若最新版本本来就已经确认该内容可用，
  一次临时失败后的同内容重取只是恢复到既有证据，不产生新版本。
- 公告字段修订时若某个附件同时下载失败，新版本会如实记录该附件“本次
  不可用”，但旧版本和 blob 中的历史证据仍然完整保留。

## 数据与一致性

- `tenders`：当前公告，始终指向最新成功提交的版本。
- `tender_versions`：不可变版本历史，保存完整公告字段与差异明细。
- `version_attachments`：每个版本当时的附件集合及可用状态。
- `attachment_blobs`：按内容哈希寻址的不可变附件字节。
- `collection_events` / `collection_attachments`：每一次采集尝试及逐附件观测。
- `attachments`：为兼容旧用法保留的“最近已知证据”缓存；失败下载只更新
  `last_fetch_state`/`last_fetch_error`，不清空已确认内容。

`upsert` 的全部写入（blob、版本、版本附件、当前公告、采集观测）处于同一个
SQLite 事务。写入中断会整体回滚；重试只会补上缺失的那一次版本，不会产生
半写入或重复版本。`get_timeline()` 会调用 `verify_consistency()` 检查版本
序号、当前公告与最新版本的一致性、附件哈希与 blob 的对应关系等。

旧数据库（最初的 `tenders`/`tender_versions`/`attachments` 表结构）会在
首次打开时在事务中自动升级：历史版本行全部保留；旧版本中从未保存的字段
标记为 `complete=False` 并附 `legacy_import` 原因，当前公告及旧附件缓存会被
导入最新版本与不可变 blob 表，不会丢数据。

## 测试

```bash
PYTHONPATH=src python3 -m pytest -q
```

测试覆盖多轮采集、附件新增/替换/消失、首次不可用后补采、临时失败不覆盖
历史证据、部分附件失败伴随公告修订、故障注入与事务回滚、旧库升级、重启
后持久化，以及 CLI 时间线入口。
