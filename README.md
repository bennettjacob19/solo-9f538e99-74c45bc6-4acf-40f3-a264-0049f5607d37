# C 编译缓存服务（FastAPI + GCC）

板卡头文件更新后，固件 CI 无需整体重编：本服务把每个 `.c` 的 GCC 编译结果
（`.o`）缓存起来，只依据**源码 + 目录搜索顺序 + 实际用到的头文件内容**判断
缓存是否有效。命中缓存时**完全不启动 GCC**，直接返回上次产物。

交互式 API 文档：服务启动后访问 <http://localhost:8000/docs>（Swagger UI），
或 <http://localhost:8000/redoc>。

## 1. 快速开始（docker compose）

```bash
docker compose up --build
# 服务监听 http://localhost:8000，缓存持久化在 compose volume cache-data 中
```

无 Docker 时本地运行（需要本机已装 gcc）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CCACHE_CACHE_DIR` | `/data/cache` | 缓存产物目录（每个条目一个子目录） |
| `CCACHE_GCC_BIN` | `gcc` | GCC 可执行文件 |
| `CCACHE_GCC_TIMEOUT` | `30` | 单次编译超时（秒） |

## 2. 接口

### `POST /compile`（`multipart/form-data`）

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `source` | 是 | 一个 `.c` 源文件 |
| `groups` | 是 | 逗号分隔的头文件目录（分组）名，如 `board,arch` |
| `search_order` | 是 | 逗号分隔的目录搜索顺序，优先级从高到低，必须是 `groups` 的子集排列 |
| `defines` | 否 | 逗号/换行分隔的宏，按 `-D` 传入，如 `BOARD_A,FEATURE=2` |
| `extra_flags` | 否 | 逗号/换行分隔的额外 GCC 参数 |
| `headers` | 否 | 头文件，可多个；上传文件名必须是 `<分组名>/<头文件名>`，各目录内**平铺** |

头文件在源码中统一以**尖括号 + 文件名**包含，例如 `#include <util.h>`，
由服务按 `search_order` 生成 `-I` 搜索路径。支持任意深度的嵌套 `#include`，
也支持 `#if/#ifdef` 按宏值选择不同头文件。

**响应**

- 成功：`200`，body 为 GCC 生成的 ELF `.o`（文件名取源文件名 stem + `.o`）
- 编译失败：`422`，JSON 诊断，含 `command`、`returncode`、`stderr`、`stdout`
- 工作区准备失败：`500`，JSON `detail` + `reason`（携带底层 I/O 错误原因），
  响应头 `X-Cache-Reason: workspace_prep_failed`
- 参数错误：`422`，JSON `detail`

**响应头（每个响应都带 `X-Cache`）**

| 头 | 含义 |
| --- | --- |
| `X-Cache: HIT` / `MISS` | 是否命中缓存。HIT 路径不启动 GCC |
| `X-Cache-Key` | 缓存键（源码哈希 + 搜索顺序 + 宏/参数 + GCC 版本） |
| `X-Cache-Reason` | MISS 原因：`compiled` / `compilation_failed` |
| `X-Used-Headers` | 本次编译实际（传递）用到的头文件列表 |
| `X-Compile-Ms` | 仅 MISS，GCC 实际耗时（毫秒） |

辅助端点：`GET /health`（GCC 版本、缓存目录）、`GET /stats`（命中数、命中率、
失效原因统计、条目数、共享头文件存储统计）。

### curl 示例

```bash
curl -D - -o main.o http://localhost:8000/compile \
  -F "source=@main.c" \
  -F "groups=board,arch" \
  -F "search_order=arch,board" \
  -F "defines=BOARD_A" \
  -F "headers=@headers/board/util.h;filename=board/util.h" \
  -F "headers=@headers/arch/plat.h;filename=arch/plat.h"
```

或直接使用 `scripts/example.sh`。

## 3. 缓存语义（与需求逐条对应）

缓存键只包含：源文件哈希、目录搜索顺序、`defines`/额外参数、GCC 版本。
头文件内容**不进键**，改为在命中校验阶段逐条核对，从而实现：

1. **源码与搜索顺序相同，只改未用到的头文件 → 直接 HIT，不启动 GCC。**
   GCC 以 `-MMD -MP -MF` 生成依赖文件，服务据此记录**预处理器实际用到的
   所有传递依赖头文件**及其 SHA-256；未出现在依赖中的头文件任意增删改都不
   影响命中。
2. **已用头文件被删或改 → MISS。** 校验时要求每个已用头文件仍存在且
   字节级哈希一致，否则重新编译并覆盖该条目。
3. **更靠前的目录新增同名头文件 → MISS。** 元数据记录每个已用头文件当时
   解析到的目录下标；校验时若任何更靠前目录出现同名头文件（会发生遮蔽），
   即判定失效。
4. **嵌套包含与按宏选择。** 实际包含集合完全由 GCC 预处理结果决定，
   `#if defined(...)` 选到哪个头就记录哪个；`defines` 不同则缓存键不同，
   互不污染。
5. **编译失败返回诊断且不写缓存；改回原输入仍命中旧缓存。** 失败响应为
   `422` + stderr 诊断；因为失败不落盘，恢复输入后命中的是上一次成功产物。

缓存目录布局（内容寻址，对象文件不可变，可安全并发读取）：

```
<CCACHE_CACHE_DIR>/
  ├── objects/<sha256>.o        # 不可变的 GCC 产物（按内容哈希去重）
  └── <cache-key>/
        ├── meta.json           # 搜索顺序、已用头文件(目录下标/文件名/SHA-256)、宏、GCC 版本
        └── object.o -> ../objects/<sha256>.o   # 符号链接
```

## 4. 并行请求的共享头文件存储

同一批固件模块并行编译时，各请求往往上传完全相同的板卡头文件。为避免服务
为每份源码重复写一遍相同正文，进程内维护一个**内容寻址的共享头文件存储**
（`app/shared_store.py`）：

- 键为 `(分组名, 文件名, 内容 SHA-256)`。并行请求上传的头文件若分组、文件名
  和内容一致，GCC 临时输入**只写一份正文**，各请求在自己的工作目录内通过
  **硬链接**（平台不支持时回退符号链接/拷贝）引用同一 blob，GCC 看到的仍是
  普通文件 `workdir/<group>/<file>`。
- **上传暂存另计**：每个请求仍独立读取并校验自己的上传字节，各自持有
  `contents` 映射；共享的只是落盘后的 GCC 输入正文。
- **不同缓存键照常并行**：同组同名但内容不同的版本各有独立 blob，可同时被
  不同请求使用，GCC 编译本身完全并行；仅在首次落盘某个新 blob 时短暂串行，
  且只可能发生在内容确实相同的上传之间，确保任何情况下正文都只写一份。
- **每个请求仍按自己的目录顺序选头文件**：`-I` 搜索路径、GCC 的 `-MMD -MP`
  依赖记录（沿用原分组与文件名）以及缓存命中校验逻辑均保持不变。
- **引用计数 + 租约**：每个请求为其用到的 blob 持有租约，在准备失败或编译
  结束后的 `finally` 中统一释放；引用计数归零（最后一个使用者退出）时才删除
  共用文件。`/stats` 的 `shared_headers` 字段暴露当前 blob 数、累计写入数、
  去重复用次数与回退次数，正常请求结束后 `active_blobs` 应回到 0。
- **准备工作区失败会返回原因**：共享正文写入或链接失败、`source.c` 暂存失败
  均返回 `500` 与具体 `reason`，且失败请求已获取的租约随 `finally` 回滚，
  不留残留 blob。

## 5. 设计说明与边界

- 每次请求使用独立临时工作目录，头文件按分组平铺落盘（正文为共享 blob 的
  硬链接），编译结束即清理；分组名与头文件名做白名单校验，拒绝路径穿越
  （`..`、`/`、`\`）。
- 编译以子进程超时保护（`CCACHE_GCC_TIMEOUT`），超时返回 `504`；子进程通过
  线程池执行，不阻塞事件循环。
- 并发安全：按缓存键加异步锁并 double-check，相同键的并发请求只编译一次，
  其余直接等锁后命中；不同键互不阻塞。共享头文件存储以线程锁保护引用计数，
  blob 内容寻址、文件名含哈希因而不可变；对象文件同样内容寻址、永不原地
  改写，响应流式传输不会被并发重编译截断。
- 失败不缓存（无意义且可能放大错误）；同一缓存键重新编译成功后覆盖旧条目。
- 系统头文件（`/usr/include` 等）不在请求范围内，不参与失效校验；
  GCC 版本变化会使旧键自然失效。
- 共享存储随进程存活，服务关闭时（lifespan 钩子）清理其临时目录。

## 6. 自测

```bash
pip install -r requirements.txt pytest
pytest tests/ -v
```

测试覆盖（25 个用例，均已通过）：首次 MISS/重复 HIT、未用头变更保持 HIT、
已用头修改/删除失效、删除后恢复仍命中、前置目录同名遮蔽、搜索顺序反转、
嵌套传递依赖跟踪、宏值选头与宏参与缓存键、语法错误诊断与旧缓存保留、
参数校验、无头文件源码、12 线程并发同键只编译一次且产物完整、`/stats` 统计；
共享存储专项：相同内容只写一份正文并按硬链接共享、同名不同版本并存且并行
编译、不同缓存键互不阻塞、写盘失败回滚租约、单方编译失败释放占用而他方
存活、准备失败返回原因且无租约泄漏、最后一个使用者退出后才删除共用文件。
