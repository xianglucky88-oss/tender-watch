# Tender Watch

招投标文件变更追踪与证据归档的可运行最小实现。

## 责任边界

- `fetching.py`：负责 URL 或 fixture 内容获取。
- `extract.py`：负责 HTML 元数据与附件链接解析。
- `dedup.py`：负责文件指纹和记录指纹。
- `storage.py`：负责 SQLite 表结构和幂等写入。
- `pipeline.py`：只负责编排上述职责。

运行 fixture：

```bash
PYTHONPATH=src python -m tender_watch.cli --fixture fixtures/sample_notice.html --database /tmp/tender-watch.sqlite3
```
