# 查尔斯信号监控系统

基于《查尔斯交易实操手册》策略规则（保底策略 / 归汤策略 / 交易模板）的自动化信号监控 + 企业微信推送系统。

监控对象：**BTC**（BTC/USDT）与 **黄金**（PAXG/USDT 代理，PAXG 严格锚定黄金 1:1 盎司，走势与伦敦金一致）。
数据源：Gate.io 永续合约 K 线（主，贴近实际交易标的，国内可直连）、Gate.io 现货 / OKX / Binance（自动备援）。

---

## 一、部署前准备

1. **Python 3.8+** 环境（Linux / macOS / Windows 均可）。
2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```
3. **配置企业微信机器人**（必填）：
   - 打开企业微信群 → 右键群聊 → 「添加群机器人」→ 获取 Webhook 地址；
   - 将地址填入 `config.yaml` 的 `wecom_webhook`，或设置环境变量：
     ```bash
     export WECOM_WEBHOOK="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
     ```

## 二、快速开始

```bash
# 1. 自测：拉真实行情验证数据链路和信号引擎（无需 webhook）
python test_signal.py

# 2. 发送测试消息到企业微信（验证推送）
python pusher.py

# 3. 正式运行（前台）
python monitor.py

# 只扫描一次（调试用）
python monitor.py --once
```

## 三、信号规则（规则化实现，来源手册）

| 策略 | 规则 | 对应手册 |
|------|------|---------|
| 保底策略 | 1H 趋势方向 + 15M BOS/MSS 转势确认 + FVG 过滤，目标 1:5 | 分册3 §3.1 |
| 归汤策略 | 价格影线穿过关键水平（swing 聚类）后收回 = 假突破，反手 | 分册3 §3.2 / 分册2 §2.4 |
| 交易模板 | 4H 趋势（定方向）+ 15M 同向 BOS/MSS 共振 | 分册3 §3.3 |
| 结构原语 | BOS 实体收穿 / MSS 回踩型转势 / FVG 三K缺口 / 0.5 回踩 | 分册2 |

> ⚠️ 手册中「数结构」「倒V回踩」「庄家意愿」等需盘面经验的主观判断已做**规则化近似**，
> 信号用于**提醒人工复核**，不构成直接交易指令。

## 四、运行方式

### 方式 A：前台运行（最简单）
```bash
nohup python monitor.py > monitor.log 2>&1 &
```

### 方式 B：systemd 常驻服务（Linux 推荐）
`/etc/systemd/system/charles-monitor.service`：
```ini
[Unit]
Description=Charles Signal Monitor
After=network-online.target

[Service]
WorkingDirectory=/你的路径/查尔斯信号监控系统
ExecStart=/usr/bin/python3 monitor.py
Restart=always
EnvironmentFile=/你的路径/查尔斯信号监控系统/monitor.env

[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload && systemctl enable --now charles-monitor
```

### 方式 C：crontab（若你的机器支持）
```bash
# 每5分钟扫描一次（用 --once 模式）
*/5 * * * * cd /你的路径/查尔斯信号监控系统 && /usr/bin/python3 monitor.py --once >> monitor.log 2>&1
```

## 五、配置说明（config.yaml）

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `wecom_webhook` | 空 | 企业微信机器人地址（必填） |
| `symbols.btc.enabled` | true | BTC 监控开关 |
| `symbols.gold.enabled` | true | 黄金监控开关 |
| `engine.h1_lookback` | 120 | 1H 参与计算的 K 线根数 |
| `engine.pivot_radius_h1` | 3 | 1H swing 高低点半径 |
| `engine.fvg_min_pct` | 0.05 | FVG 最小缺口幅度(%) |
| `engine.trend_ma` | 50 | 趋势均线周期 |
| `scanner.scan_interval_seconds` | 300 | 扫描间隔（秒） |
| `scanner.cooldown_hours` | 6 | 同信号冷却时长（小时） |
| `scanner.push_test_on_start` | true | 启动时发送测试消息 |

## 五·五、模拟盘（Paper Trading）

信号命中后系统**自动开模拟单**（不涉及真实资金），用于练习验证策略胜率。

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `paper_trading.enabled` | true | 模拟盘总开关（false 关闭） |
| `paper_trading.initial_balance` | 100 | 初始模拟资金(USDT) |
| `paper_trading.leverage` | 100 | 杠杆倍数（100x：价格反向波动 1% 即爆仓） |
| `paper_trading.risk_per_trade_pct` | 1.0 | 每笔风险占余额%(手册单笔风控1%) |
| `paper_trading.sl_pct` | 0.8 | 止损距离%(入场价上下)，须小于爆仓线 100/杠杆 % |
| `paper_trading.tp_rr` | 2.0 | 止盈 = 止损距离 × 倍数（0.8%×2=1.6%） |
| `paper_trading.max_hold_hours` | 24 | 最长持仓时间，超时按市价强平 |
| `paper_trading.state_file` | paper_state.json | 模拟账户状态文件 |

行为说明：
- 信号命中 → 推送提醒 + 自动开模拟仓（同品种已有同向持仓则忽略）；
- 反向信号 → 先平旧仓（推送平仓记录）再反手开新仓；
- 每轮扫描检查持仓：触发止损/止盈/爆仓/超时 24h → 自动平仓并推送结果；
- 爆仓规则：价格反向波动达到 100/杠杆 %（100x → 1%）时亏光保证金强平，止损距离必须小于该值才会先止损（当前 0.8% < 1%）；
- 模拟账户状态保存在 `paper_state.json`，GitHub Actions 云端运行会自动提交回仓库，状态不丢；
- 推送消息中会附带模拟账户余额、保证金与持仓，方便手机查看。

## 六、风险提示

- 本系统为规则化辅助提醒工具，信号质量取决于策略参数与市场环境；
- 手册要求所有策略**先回测半年以上再实盘**；
- 交易有风险，入市需谨慎；请自行做好仓位与风险管理（风控→仓位→心态→操作）。

