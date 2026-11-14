# pm5m — Polymarket BTC 5 分钟盘口 · 纸面（Paper）测试模块

**范围**：用真实行情数据做策略模拟，**不发送任何真实订单**。私钥只用于一次性推导 API 凭据（默认用完即丢弃）。
实盘下单是另一个模块，建议等纸面数据证明有优势后再做。

## 先看这三点（会影响你怎么用它）

1. **结算规则已变**：自 2026-08-14 起，国际站 5 分钟盘口按 **Chainlink 60 秒 TWAP** 结算——
   起点 B = 窗口开始前 60 秒的均值，终值 = 窗口最后 60 秒的均值，终值 ≥ B 则 Up。
   所以「后段翻转」比点比较规则更难，模型（`model.py`）已按此实现。
2. **Chainlink 数据确实需要 API 凭据**：官方已把价格推送迁到 PolyBolt（`wss://ws-live-v2.polymarket.com/ws`），
   需要用 CLOB API 凭据鉴权；API 凭据由钱包私钥签名推导。流程：私钥 → API key/secret/passphrase → PolyBolt 鉴权 → Chainlink 现货 + TWAP60。
   不想粘贴私钥也可以手动填已有的 API 凭据。
3. **Binance 不是必需，但建议开着当参考**：结算只看 Chainlink。Binance 的价值是（a）领先 Chainlink 约几秒，
   （b）交叉验证数据是否卡住，（c）事后研究基差能否预测翻转。当前版本只**记录**基差，不参与下单决策。

## 安装与运行

### 服务器（一条命令）

在 Linux 服务器上（需要 systemd）执行：

```bash
sudo bash -c "curl -fsSL https://raw.githubusercontent.com/hrf100/pm5m/main/deploy/install.sh | bash"
```

脚本从 [hrf100/pm5m](https://github.com/hrf100/pm5m) 的 `main` 拉取代码。已经把目录放在服务器上时，也可以在该目录执行 `sudo bash deploy/install.sh`。

脚本会装 Python 3.9+ 与依赖、创建系统用户 `pm5m`、代码放到 `/opt/pm5m`、数据放到 `/var/lib/pm5m`、生成 `/etc/pm5m.env`（随机网页密码，权限 600）、注册并启动 `pm5m.service`。应用监听 `0.0.0.0:8080`，浏览器打开 `http://服务器IP:8080`。重复执行会按 GitHub `main` 升级，不会改已有密码和私钥；若旧配置还写着 `127.0.0.1`，会改成直接监听。

装完：用户名 `admin`，初始密码在安装输出和 `/etc/pm5m.env` 的 `PM5M_PASSWORD`。之后在页面「钱包 → 修改登录密码」改掉的密码存在数据目录的 `web_password.txt`，重启后优先于环境变量。私钥只写 `PM5M_PRIVATE_KEY`，然后 `sudo systemctl restart pm5m`。资金地址和账户类型会自动识别。云厂商安全组要放行 TCP 8080。日志：`journalctl -u pm5m -f`。

### 本地开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pm5m --host 127.0.0.1 --port 8080 --data-dir ./data
```
首次启动会生成网页登录密码（用户名 `admin`），打印在终端并存到 `data/web_password.txt`。也可用环境变量 `PM5M_PASSWORD`。

### 在欧洲服务器上部署

- **先确认服务器 IP 没被 Polymarket 限制**：页面顶部有「地域」状态（调用官方 geoblock 接口），「连接自检」也会检查。
  据我查到的官方文档，部分欧洲国家（例如法国、英国、意大利等）的 IP 受限，以实时返回为准。请租用前先核对，不要用代理绕过限制。
- **服务器时钟必须准**（chainlink/窗口边界都依赖它）：`chronyc tracking` 或 `timedatectl`，偏差 > 1 秒会在自检里报错。安装脚本会尝试打开 NTP。
- 网页地址是 `http://服务器IP:8080`，需要登录。钱包只在页面填私钥。所有写操作需要 `X-Requested-With` 头，防跨站请求。

## 钱包

页面钱包栏只填私钥，保存后不留在服务器。签名地址由私钥推出；资金地址从 Gamma 公开资料的 `proxyWallet` 读取；账户类型按该地址的合约代码识别（EOA / 代理 / Safe / 充值）。也可以把同一把私钥写在 `PM5M_PRIVATE_KEY`。

`PM5M_FUNDER` 和 `PM5M_SIG_TYPE` 只有两个都写上时才覆盖自动结果。

## 页面功能

- 当前盘口：倒计时、B、Chainlink 现货、TWAP60、Binance 及基差、实时 σ、模型 P(Up)/P(Down)、各方向买一/卖一。
- 图表 1：Up/Down 买一（及卖一）价格走势，叠加挂单（▲）与成交（●）标记；图表 2：现货/TWAP60/Binance 相对 B 的差值，竖线标出最后 60 秒。
- 策略面板：参数、开始/停止、成绩（成交率、成交后翻转率 vs 盈亏平衡、**未成交单的翻转率**，用于量化逆向选择）。
- 导出 CSV、**重置记录**（默认先归档到 `data/archive/<时间>/`，可选同时清除窗口序列）。

## 策略与纸面撮合模型

- 每 0.25 秒评估：剩余时间在 `[min_remaining, max_remaining]`，某一侧买一价在 `[min_price, max_price]`，
  且 `模型概率 × 折扣 ≥ 价格 + 边际` → 在买一**排队挂单**，每单约 5 美元（`unit_usd`），持有到结算。
- 成交只来自真实成交流：`conservative` 模式下排在前面的挂单量要先被成交流消耗，之后才轮到你；
  成交价低于你的挂价视为全部成交；挂单后 `latency_ms` 内的成交忽略；不考虑你的单对盘口的影响。
- 结算：窗口结束后用 TWAP60 流计算终值 vs B 得到临时结果，再轮询 Gamma 官方结果核对，不一致会记录并按官方结果重算。
- 每个信号（含未成交）都会记录结果，用来估计逆向选择：成交单的翻转率 vs 未成交单的翻转率。

**局限（纸面结果必然偏乐观，需要打折看）**：忽略撤单对排队的帮助（偏保守）和你自己的冲击（偏乐观）；
延迟是固定值；启动后的第一个窗口拿不到 B，会自动等下一个窗口；模型是零漂移布朗运动，BTC 的跳空会让真实尾部更肥。

## 数据文件（`data/`）

`settings.json` 参数｜`secrets.json` 凭据（600）｜`paper_orders.json/.csv` 订单｜`events.jsonl` 下单/成交/撤单/结算事件｜
`series/<slug>.json` 每个窗口 1 秒级序列（现货、TWAP、Binance、四条盘口价、模型概率），可用于之后的回测。

## 测试与已验证/未验证

```bash
python -m unittest discover -s tests -v
```
- **已用离线测试验证**：概率模型（含蒙特卡洛对照）、盘口与消息解析、L2 HMAC 签名、加密存储、重置归档、纸面撮合与结算、
  整个窗口的端到端回放、Web 处理函数（登录/限流/CSRF/钱包校验/状态/导出/重置）。
- **未能在开发环境验证（没有外网）**：与 Polymarket/Binance/Polygon RPC 的真实连接、EIP-712 签名推导 API 凭据（`eth-account` 测试在装了依赖的机器上才会运行）、
  PolyBolt 鉴权帧格式、市场频道的动态订阅。请先点页面的「运行连接自检」，并看顶部各数据源状态灯；出问题时错误信息会显示在「诊断」栏。
- Deposit Wallet（类型 3）等新账户形态的 API 行为有已知变动，若余额显示失败，以「诊断」栏的错误为准，并可用链上 pUSD 余额作对照。

## 安全清单

- 服务直接监听 `0.0.0.0:8080`，用登录密码保护。防火墙和云安全组只放行需要的端口（含 8080）。
- 用专用小额钱包；不勾选「保存私钥」；设置 `PM5M_SECRET_PASSPHRASE`。
- `data/` 目录权限限制为运行用户；不要把 `secrets.json`、`web_password.txt` 提交到版本库或备份到公共位置。
- 本模块不构成投资建议；费率、规则、接口可能调整，以官方文档为准。

## 阶段 1：离线样本外检验（研究模块）

```bash
# 1) 冻结规则（只能做一次；之后要改就新建 v2 文件，不要改 v1）
python -m pm5m.research --freeze --rules research_rules.json
# 2) 用真实窗口序列运行（默认读 ./data/series，按时间 70/30 切分）
python -m pm5m.research --rules research_rules.json --series ./data/series
# 3) 仅测试流水线（合成数据，结果与市场无关）
python -m pm5m.research --synth 1500
```

流水线做了什么：
- 特征：基础模型 p_base（TWAP60 结算的解析概率）、30 秒动量 z、Binance–Chainlink 基差 z。特征在第 i 行只使用时间 ≤ t_i 的数据（测试已验证）。
- 模型：逻辑回归校准 `logit(p_base), mom, basis`，只在训练集（前 70% 窗口）拟合；σ 也只用训练集估计。
- 评估：测试集上比较 Brier（基础模型 / 校准后 / 市场中间价）、校准表、taker 吃单模拟（扣费，每窗口最多一笔），用按窗口的 bootstrap 给出 95% 区间。
- 结论规则：笔数 < `min_trades`（默认 200）一律判为「结论无效」；只有 95% 区间全部为正才判为「候选优势」，且仍需在之后的新数据上确认。

合成数据的检验结果（仅说明代码行为）：
- 零优势世界（lead=0）：基差系数 ≈ 0，流水线不报优势，零优势对照的笔数为 0（符合预期）。
- 植入 2 秒领先信号的世界：基差系数被识别为 0.646，但 450 个测试窗口下交易收益区间仍包含 0。说明真正的小优势需要远多于此的样本。

## 连接自检（服务器上先跑这个）

```bash
cd /opt/pm5m && . .venv/bin/activate
export PM5M_PRIVATE_KEY=0x...        # 可选：只用于推导 API 凭据，不会保存或打印
python -m pm5m.selftest
```
脚本依次检查：地域限制、时钟、当前 Gamma 市场、API 凭据推导、PolyBolt（鉴权、订阅确认、25 秒内各频道帧数、首帧字段、TWAP 与现货差值）、Binance 逐笔、CLOB 余额、链上 pUSD、CLOB 市场频道。
结果写入 `pm5m_selftest_report.txt`，把这个文件内容发回即可。

注意：CLOB 市场频道的订阅格式尚未对照官方文档，自检中该项为 WARN 时以实际返回为准。


## 直接访问

浏览器打开 `http://服务器IP:8080`，用户名 `admin`。安装脚本在本机防火墙已开启时放行该端口；阿里云、AWS 等安全组需要自己放行 TCP 8080。

- 私钥在页面钱包栏填写，只用来换 API 凭据，不保存。
- 明文网页上提交私钥会被拒绝。
- 登录失败 10 次后，同一 IP 锁 5 分钟。可选 `PM5M_IP_ALLOWLIST` 限制来源 IP。

## 资源

页顶一栏：CPU（系统百分比和核数，小字是本进程）、内存（已用/总量）、总收、总发（开机以来的网卡字节，小字是当前 KB/s）、盘余。数据来自 Linux `/proc`。
