# mitene_download

Download medias from https://mitene.us/ or https://family-album.com/ to keep a local backup.

**101 教學 →** https://forumdata-collab.github.io/mitene_download/

## Fork 新增功能 (v0.9.0, 基於 upstream v0.7.0)

| 參數 | 預設 | 作用 |
|---|---|---|
| `--cooldown N` | 0.3s | 下載之間 jitter sleep；429/5xx 自動指數退避重試（最多 4 次），防 IP 被封 |
| `--no-organize` | 開 | 關閉 YYYY-MM/ 年月子目錄（舊平面輸出） |
| `--gdrive-upload REMOTE:FOLDER` | 關 | 下載完成後 `rclone move` 到 GDrive |
| `--nocomments` | 關 | 唔下載留言 .md 檔 |

- **年月分類**：按拍攝時間自動歸入 `out/2024-05/`，開頭自動 migrate 舊平面檔
- **Log 統計**：每次執行喺 `out/download.log` 記低 `added/skipped/failed`
- **斷點續傳**：已有檔案 skip，重跑只補漏
- 唔用新 flag = 等同原版行為（向後兼容）

## Usage

Install with `pip install git+https://github.com/forumdata-collab/mitene_download.git`.

From mitene app, invite a family member for the web version and copy the URL ( that should be something like `https://mitene.us/f/abcd123456` )

Run the script with `mitene_download https://mitene.us/f/abcd123456`, using the URL from previous step.

This will download all photos and video in `out` folder. Some text files will be created with the comments.

GDrive version: append `--gdrive-upload gdrive:mitene-backup` (requires `rclone config`).

## Self-check

```bash
python3 test_self.py   # no network needed: organize/migrate/log/cooldown
```
