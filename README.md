# Hudoo SEO Desk · 互旦 SEO 工作台

一个跑在本机的 SEO 数据工作台。开一个本地网页当界面，双击即用，不依赖任何云端服务。

目前包含：

| 模块 | 说明 | 状态 |
|---|---|---|
| **拓词 / 补搜索量** | 直连 Google Ads API 的关键词规划师，一次调用同时拿到搜索量、竞争程度、页首出价和近 12 个月分月数据 | ✅ 可用 |
| 排名监控 | 关键词排名追踪与历史曲线 | 🚧 规划中 |
| Skill 执行引擎 | 用 Claude API 跑 SEO 审计 / 内容 / GEO 流程 | 🚧 规划中 |

## 为什么不是直接用关键词规划师网页版

网页版有三个绕不开的麻烦：每建一个方案地区都会重置回「中国」，对外贸场景必须每次手动改；导出的 CSV 是 UTF-16 + Tab 分隔，拿到手还得转码；而且拿不到分月搜索量。走 API 这三件事一次解决，且**在免费额度内**（基本访问权限每天 15,000 次操作，一万个词只消耗个位数）。

## 快速开始

```bash
git clone https://github.com/jiewebsite9-hash/hudoo-seo-desk.git
cd hudoo-seo-desk
pip install -r requirements.txt

copy config.example.yaml config.local.yaml    # Linux/macOS: cp
# 编辑 config.local.yaml 填入凭据，见下一节

python -m app.main auth      # 浏览器点一次「允许」，refresh_token 自动写回
python -m app.main           # 启动，浏览器自动打开 http://127.0.0.1:8790
```

Windows 上也可以直接双击 `start.bat`。

## 配置 Google Ads API

需要四样东西，都填进 `config.local.yaml` 的 `google_ads` 段：

| 配置项 | 从哪来 |
|---|---|
| `login_customer_id` | 你的经理账号（MCC）ID，纯数字不带横杠 |
| `developer_token` | MCC → 工具与设置 → API 中心 → 查看令牌 |
| `client_id` / `client_secret` | Google Cloud 控制台 → 凭据 → 创建 OAuth 客户端 ID → 应用类型选**桌面应用** |
| `refresh_token` | 跑 `python -m app.main auth` 自动生成 |

### ⚠️ 访问权限级别必须是「基本(Basic)」

这是最容易卡住的一步。2026-09-10 起，Google 把 API 访问权限从开发者令牌**移到了 Google Cloud 项目**上——MCC「API 中心」页面显示的级别已经不作数（页面自己的横幅就是这么写的）。

四个级别里只有「基本」能用关键词规划师：

| 级别 | 生产账号额度 | 关键词规划师 |
|---|---|---|
| 测试 | 无（只能读测试账号，测试账号没有真实数据） | ❌ |
| 探索者 | 2,880 次/天 | ❌ **planning 类服务被屏蔽** |
| **基本** | **15,000 次/天** | ✅ |
| 标准 | 不限 | ✅ |

申请路径：Cloud 控制台 → Google Ads API 概览页 → 先完成**品牌验证**（OAuth 权限请求页面：用户类型设为「外部」、发布状态设为「正式版」，填齐首页/隐私政策/服务条款/授权网域后点「验证品牌」）→ 再申请「基本」。

几个实测经验：

- 品牌验证偶尔会误报首页或隐私政策「无响应」。先自己 curl 一下确认站点正常，正常的话**直接重试**，多半是抓取端的偶发失败。
- 「首页网址未注册到您的名下」是指 Search Console 所有权。如果你已经是该域名的**代理所有者**，新建一个 URL 前缀资源（`https://www.example.com/`）通常会被**自动验证通过**，不需要往服务器放文件或改 DNS。
- OAuth 凭据必须出自**拿到基本权限的那个 Cloud 项目**，换项目建凭据权限归零。

## 配置文件与凭据

所有凭据只存在本机的 `config.local.yaml`，该文件已在 `.gitignore` 中。每一项也都支持环境变量覆盖（变量名见 `config.example.yaml` 的注释）。

界面的「设置」页只显示每项凭据**配没配**，不回传内容。

## 输出字段

| 列 | 说明 |
|---|---|
| 月均搜索量 | 低花费账号拿到的是区间中值（50 / 500 / 5000 那套），与网页版一致 |
| 搜索量档位 | 按中值还原成 `10-100` / `100-1K` / `1K+` 档 |
| 竞争程度 / 竞争指数 | 低/中/高，外加 0-100 的指数（网页版导出里没有） |
| 页首出价低 / 高 | 已从 micros 换算成账号币种 |
| 可行性评分 | 竞争程度映射：低→5 / 中→4 / 高→3 |
| 高价值 | 按账号币种自动套阈值 |
| 近 12 月 | `2025-09:1600\|2025-08:1300\|…`，判季节性用 |

CSV 为 UTF-8-SIG，Excel 双击直接认中文。

> **关于「高价值」列的一个已知局限**：默认阈值按通用外贸品类标定。B2B 工业/机械类关键词的页首出价普遍高出一个数量级，会把整列打成「极高」而失去区分度。这类项目请按自己的行业重新标定 `app/modules/keywords/gkp.py` 里的 `VALUE_LINE`，或直接改看「可行性评分」。

## 关于 skills 目录

`skills/` 故意留空。程序执行的 SEO 方法论内容包属于使用方的内部资产，不随本仓库分发，由各自在本机提供。查找顺序见 `skills/README.md`。

## 开发

```
app/
├── main.py                  入口（serve / auth / check）
├── server.py                本地 HTTP 服务，标准库 http.server
├── config.py                配置加载，环境变量 > config.local.yaml
├── jobs.py                  后台作业 + 增量日志
├── auth.py                  OAuth 授权
└── modules/keywords/        Google Ads API 取数
web/                         界面，无框架
```

后端只用标准库起服务，第三方依赖仅三个（`google-ads` / `PyYAML` / `anthropic`），打包体积和启动速度都因此受益。

## 许可

MIT
