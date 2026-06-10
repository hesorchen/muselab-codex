# Files API

> [English](backend-files.md)

Files API（`/api/files/*`）是 muselab 向浏览器暴露的唯一文件系统接口。所有读取、写入、搜索和删除操作都经由这个路由处理——它能访问的目录仅限归档根目录（`MUSELAB_ROOT`），交换单元为完整文件（非流式字节范围）。浏览器从不直接得到文件系统路径；每个路径字符串在任何 OS 调用发生前都必须经过 [`safe_resolve`](#safe_resolve-深度解析) 的验证与解析。

鉴权细节——token 格式、恒定时间比较、访问日志中的 token 脱敏——请参阅 [backend-security_zh.md](backend-security_zh.md)。

---

## 接口参考

全部 18 个接口共用 `/api/files` 前缀（[`backend/files.py:L16`](../backend/files.py#L16)）。
鉴权方式：**header** = 仅 `X-Auth-Token` header；**query** = 仅 `?token=` 查询参数。查询参数方式适用于无法发送自定义 header 的浏览器场景（`<img src>`、`<iframe src>`）。

### 读取 / 检查

| 方法 | 路径 | 用途 | 鉴权 |
|------|------|------|------|
| `GET` | `/api/files/list` | 列出目录条目。参数：`path`、`show_hidden`。返回 `{root, path, entries[], truncated}`。最多 500 条；即使 `show_hidden=true`，回收站目录始终排除在外。（[`L360–L393`](../backend/files.py#L360)） | header |
| `GET` | `/api/files/read` | 以 `text/plain` 格式读取文本文件。拒绝已知二进制扩展名，以及前 4 KB 中非 UTF-8 字节超过 5% 的文件。大小上限：2 MB。（[`L583–L618`](../backend/files.py#L583)） | header |
| `GET` | `/api/files/stat` | 获取单个路径的轻量元数据：`{path, name, is_dir, size, mtime}`。（[`L621–L644`](../backend/files.py#L621)） | header |
| `GET` | `/api/files/raw` | 流式传输原始文件供浏览器渲染。图片 / PDF / 媒体文件以内联方式提供；HTML / SVG 内联提供并附加严格 CSP；其他格式强制返回 `application/octet-stream` 附件。`Cache-Control: no-cache` 强制条件 GET。（[`L656–L709`](../backend/files.py#L656)） | query |
| `GET` | `/api/files/download` | 以 `application/octet-stream` 和 `Content-Disposition: attachment` 强制下载任意文件。（[`L712–L721`](../backend/files.py#L712)） | query |
| `GET` | `/api/files/xlsx` | 只读 XLSX 预览，返回结构化 JSON。最多 20 个 sheet、500 行、50 列；单元格截断至 500 字符。按需加载 `openpyxl`。（[`L406–L471`](../backend/files.py#L406)） | header |
| `GET` | `/api/files/csv` | 分页 CSV/TSV 预览。参数：`offset`、`limit`（默认 200，最大 1000）。返回嗅探到的分隔符、表头行及 `total_rows`。（[`L485–L572`](../backend/files.py#L485)） | header |
| `GET` | `/api/files/grep` | 全文搜索（纯 Python 实现，无 shell 依赖）。最短查询：2 字符；时间预算：8 秒；单文件上限：1 MB。返回 `{hits[], truncated}`，每条命中包含 `{path, name, line, snippet}`。（[`L1193–L1265`](../backend/files.py#L1193)） | header |
| `GET` | `/api/files/search` | 文件名 / 目录名子字符串搜索（不读取文件内容）。返回 `{entries[], truncated}`。（[`L1268–L1300`](../backend/files.py#L1268)） | header |

### 写入 / 变更

| 方法 | 路径 | 用途 | 鉴权 |
|------|------|------|------|
| `PUT` | `/api/files/write` | 覆盖或创建文件。请求体：`{path, content}`。通过临时文件 + rename 原子写入。写入上限：10 MB。拒绝写入回收站目录。（[`L736–L762`](../backend/files.py#L736)） | header |
| `POST` | `/api/files/upload` | Multipart 上传。表单字段：`path`（目标目录）、`file`。只保留 basename，屏蔽危险扩展名（`.exe`、`.dll`、`.so`、`.dylib`、`.scr`、`.ps1` 等）及敏感文件名。上限：100 MB（可通过 `MUSELAB_MAX_UPLOAD_MB` 覆盖）。同名已有文件在 rename 前会软删除到回收站。（[`L776–L849`](../backend/files.py#L776)） | header |
| `POST` | `/api/files/mkdir` | 创建目录（`parents=True`）。（[`L983–L988`](../backend/files.py#L983)） | header |
| `POST` | `/api/files/rename` | 移动 / 重命名文件或目录。请求体：`{src, dst}`。源不存在返回 404；目标已存在返回 409。（[`L996–L1008`](../backend/files.py#L996)） | header |
| `POST` | `/api/files/copy-bak` | 将文件复制为 `<name>.bak`（或 `.bak.2`、`.bak.3`……最多 999）。仅限文件；目标名称由服务端自动派生。（[`L1047–L1086`](../backend/files.py#L1047)） | header |
| `DELETE` | `/api/files/delete` | 默认软删除（移入 `.muselab-dustbin/`）。`?permanent=true` 为硬删除。拒绝以回收站目录本身为目标。（[`L856–L889`](../backend/files.py#L856)） | header |

### 回收站管理

| 方法 | 路径 | 用途 | 鉴权 |
|------|------|------|------|
| `GET` | `/api/files/trash/list` | 列出所有回收站条目，最新在前。返回 `{items[], total_size, ttl_days}`。（[`L895–L909`](../backend/files.py#L895)） | header |
| `POST` | `/api/files/trash/restore` | 将一个条目还原到原路径。目标路径已被占用时返回 409。请求体：`{trash_id}`。（[`L916–L950`](../backend/files.py#L916)） | header |
| `DELETE` | `/api/files/trash/purge` | 永久删除一个回收站条目（清单文件 + 载荷）。请求体：`{trash_id}`。（[`L953–L962`](../backend/files.py#L953)） | header |
| `DELETE` | `/api/files/trash/empty` | 永久删除全部回收站条目。（[`L965–L976`](../backend/files.py#L965)） | header |

---

## `safe_resolve` 深度解析

**函数签名：** `safe_resolve(rel: str, allow_sensitive: bool = False) -> Path`
（[`L316–L346`](../backend/files.py#L316)）

每个接口在进行任何文件系统操作之前都会调用 `safe_resolve`，它提供三层独立的防御。

### 第一层 —— 阻断 `..` 路径穿越

`(ROOT / rel).resolve()` 会规范化路径，跟踪符号链接并折叠 `..` 组件，然后检查结果是否是 `ROOT.resolve()` 的子路径（[`L339–L341`](../backend/files.py#L339)）。对于 `../../etc/passwd` 这样的请求，解析结果为 `/etc/passwd`，不在 ROOT 下，直接以 HTTP 400 `"path escapes root"` 拒绝。

### 第二层 —— 阻断符号链接逃逸

由于 `.resolve()` 会跟踪符号链接，ROOT 内指向 `/etc/shadow` 的符号链接，其真实目标路径会被检查是否在 ROOT 下——仅凭链接名称是不可信的（[`L333–L341`](../backend/files.py#L333)）。grep 接口中对每个候选文件打开前也会独立执行此检查，同时验证链接名称和解析后的目标路径（[`L1229–L1237`](../backend/files.py#L1229)）。

### 第三层 —— 敏感文件名屏蔽列表

`_is_sensitive()`（[`L305–L313`](../backend/files.py#L305)）会对两个集合和一条前缀规则进行检查（[`L286–L313`](../backend/files.py#L286)）：

**`SENSITIVE_NAMES`**（精确 basename，大小写不敏感）：
`.env`、`.env.local`、`.env.production`、`.env.development`、`.netrc`、
`.pgpass`、`.npmrc`、`.pypirc`、`.dockercfg`、`.htpasswd`、`.htaccess`、
`credentials`、`credentials.json`、`service-account.json`、`id_rsa`、`id_dsa`、
`id_ecdsa`、`id_ed25519`、`authorized_keys`、`known_hosts`、`.bash_history`、
`.zsh_history`、`.python_history`、`.node_repl_history`、`.sqlite_history`、
`.lesshst`、`.viminfo`、`.wget-hsts`、`.npm-debug.log`、`.yarn-error.log`

**`SENSITIVE_SUFFIX`**（基于扩展名）：
`.pem`、`.key`、`.p12`、`.pfx`、`.keystore`、`.jks`、`.env`

**前缀规则：** 任何以 `.env.` 开头的文件名，无论其余部分是什么，均被屏蔽（[`L309`](../backend/files.py#L309)）。

被屏蔽的路径返回 HTTP 403。此检查无论文件是否实际存在于磁盘上都会执行，因此 API 既无法读取也无法创建形如凭据的路径。

**`allow_sensitive=True`** 仅在两处使用：`trash_restore`（[`L936`](../backend/files.py#L936)），允许将之前删除的 `.env` 从回收站移回原处；以及 `copy_bak` 中的最终目标路径（[`L1070`](../backend/files.py#L1070)）。

**NUL 字节注入**会在任何路径操作之前被拒绝（[`L331–L332`](../backend/files.py#L331)）：路径字符串中出现 NUL 字节时，提前以 HTTP 400 返回。

---

## 回收站语义

删除文件时，默认操作是将其移入 `MUSELAB_ROOT` 下的 `.muselab-dustbin/` 目录，而非直接删除。回收站目录在 `/list`、`/search`、`/grep` 中始终不可见——它只通过专用的 `/trash/*` 接口暴露。

**回收站条目布局**（[`L18–L31`](../backend/files.py#L18)）：

```
<ROOT>/.muselab-dustbin/<trash_id>.json   ← 清单文件
<ROOT>/.muselab-dustbin/<trash_id>        ← 载荷（文件或目录）
```

`trash_id` 格式：`<unix_ts>_<8-hex>` —— 可排序，冲突概率极低。

**`_guard_not_trash()`**（[`L39–L53`](../backend/files.py#L39)）阻止所有写入、上传、重命名和 copy-bak 操作直接以回收站目录为目标。只有 `/trash/*` 接口可以修改回收站内容。

**`trash_id` 格式校验**（[`L62–L80`](../backend/files.py#L62)）在每次还原/清除调用时使用正则 `^\d{1,20}_[0-9a-f]{8}$` 进行验证，以阻断以 `trash_id` 为载体的路径穿越攻击（例如将 `../../etc/passwd` 作为 `trash_id` 传入）。

**自动过期：** 可通过 `MUSELAB_TRASH_TTL_DAYS` 配置（默认 30 天；`0` = 禁用）。在启动时作为后台任务运行。环境变量参考请见 [configuration_zh.md](configuration_zh.md)。

**还原冲突：** 还原时若原路径已被占用，接口返回 HTTP 409。用户需先重命名或清除占用项。

---

## 特殊渲染器：xlsx 与 csv

预览面板需要结构化数据（而非原始字节）才能将电子表格和 CSV 文件渲染为交互式表格，为此提供了两个专用接口：

**`/api/files/xlsx`**（[`L406–L471`](../backend/files.py#L406)）使用 `openpyxl`（`read_only=True, data_only=True`）解析 XLSX 文件，以每个 sheet 的行数组形式返回 JSON。公式不会重新计算——只返回上次保存时的缓存值。各维度上限确保响应大小可控：最多 20 个 sheet、500 行、50 列，单元格截断至 500 字符。

**`/api/files/csv`**（[`L485–L572`](../backend/files.py#L485)）会嗅探分隔符、检测表头行，并返回一个分页窗口（`offset` + `limit`）。分页设计避免将大型 CSV 文件全量加载到内存：每次调用时流式读取到指定偏移量。

两个接口均由前端预览面板独占使用。关于面板如何根据文件扩展名选择渲染器，请参阅 [frontend_zh.md](frontend_zh.md)。

---

*另见：*[architecture_zh.md](architecture_zh.md) · [backend-security_zh.md](backend-security_zh.md) · [configuration_zh.md](configuration_zh.md)
