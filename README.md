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

## v0.9.2 配套工具（2026-08-20）

| 工具 | 作用 |
|---|---|
| `stream_gdrive.py` | 上傳前 **name+size 去重**（GDrive folder listing 快取，同名同 size skip，重跑零重複）；`socket.setdefaulttimeout(120)` 防 hung upload 卡死成條 pipeline |
| `check_years.py` | **按年檢測**：逐年份 dry-run + GDrive 實數 → `.album_counts.json`（Web UI Check 按鈕同 runner 共用同一份數據） |
| `sync_year.py` | **半年度續存（v0.9.3 逐月制）**：每日一個半年度（2021 H1→H2→2022 H1…）；入面每個月順序做 **下載 → 上傳 → 覆查本地清零 → 刪 VM 檔 → 下一個月**（daemon 只喺 upload 成功先 unlink，失敗嘅檔留喺 VM 唔會誤刪）；state 記住已完成月份，斷咗聽日續嗰個月唔會重下載；flock 防 check/sync 互撞，完成年份由 cache 跳過 |
| `dedupe_cleanup.py` | **一次性舊重複清理**：按 (folder, name) 分組，md5 相同先刪、每組留一（2026-08 實測清 ~9.5k 檔 / ~37GB） |

**GDrive 結構 = 原生年月 mirror**：`mitene-backup/YYYY/MM/<filename>`（`YYYY-MM` 由檔案路徑解析，自動建 folder，folder id 快取喺 `.stream_folder_id`）。

**注意事項**：
- 月 folder 超過 1000 檔要 paginate（早期 count bug 令按年計數 under-count ~6k，已修）
- 改檔名/UUID 格式前諗清楚：舊檔名同新檔名唔同名 → name+size 去重失效（會產生跨格式重複）
- cron 用法：`30 3 * * * cd ~/mitene_download && python3 sync_year.py >> logs/year_sync.log 2>&1`

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
