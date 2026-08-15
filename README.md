# mitene_download

Download medias from https://mitene.us/ or https://family-album.com/ to keep a local backup.

**101 教學 →** https://forumdata-collab.github.io/mitene_download/

## Fork 新增功能 (v0.9.0, 基於 upstream v0.7.0)

| 參數 | 預設 | 作用 |
|---|---|---|
| `--cooldown N` | 0.3s | 下載之間 jitter sleep；429/5xx 自動指數退避重試（最多 4 次），防 IP 被封 |
| `--no-organize` | 開 | 關閉 YYYY-MM/ 年月子目錄（舊平面輸出） |
| `--gdrive-upload REMOTE:FOLDER` | 關 | 下載完成後 `rclone move` 到 GDrive |
| `--stream-daemon CMD` | 關 | 長駐上傳程序（推薦）：下載一個即傳一個，VM 唔保留檔案；附 `stream_gdrive.py --daemon` helper |
| `--stream-command CMD` | 關 | 每檔下載完執行指定命令，`{file}` 替換成檔案路徑（慢，每檔 spawn 新 process） |
| `--nocomments` | 關 | 唔下載留言 .md 檔 |

- **年月分類**：按拍攝時間自動歸入 `out/2024-05/`，開頭自動 migrate 舊平面檔
- **Log 統計**：每次執行喺 `out/download.log` 記低 `added/skipped/failed`
- **斷點續傳**：已有檔案 skip，重跑只補漏
- 唔用新 flag = 等同原版行為（向後兼容）

## ⚠️ v0.9.1 重要修復 (2026-08-15)

**stream-daemon race bug**：舊版 downloader 喺寫 path 去 daemon 之後**立即 unlink 本地檔**，但 daemon 由磁碟讀檔上傳——unlink 先過 upload 就 race，`stream_gdrive.py` 嘅 `os.path.exists()` check 失敗 → 靜默 skip，**檔案永久 lost**（download.log 仍報 added=N skipped=0，誤導）。實測 2837 檔全 lost。已修：downloader 唔再 unlink，由 daemon upload 成功先刪。

**驗證**：跑完 `find out -type f` 應只剩 `download.log`；GDrive 實際檔數應接近 album 總數。daemon 未退 = 仲 upload 緊，等佢自然退出或重跑補漏（已有檔 skip）。

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
