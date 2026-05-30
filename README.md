# GPU Hunter

Vast + Clore 自动扫价与抢机脚本，适合在本地电脑或服务器上长期运行。脚本会持续扫描 RTX 5090 / RTX 4090 的可租机器，在价格满足你设置的单卡小时价格后自动尝试下单。

## 平台入口

如果你还没有账号，可以从下面入口注册或打开官网：

| 平台 | 入口 |
| --- | --- |
| RunPod | [进入官网](https://runpod.io?ref=79ctz9rn) |
| Vast | [进入官网](https://cloud.vast.ai/?ref_id=107199) |
| Clore | [进入官网](https://clore.ai/register?ref=1otobybb) |

## 功能

- 支持 Vast 与 Clore 同时扫描。
- 支持 RTX 5090 / RTX 4090。
- 支持 1x / 2x / 4x / 8x / 9x+ 多卡机器。
- 支持按单卡小时价格设置上限。
- 支持代理轮询，降低单个 IP 请求压力。
- 支持 Telegram 通知，可选配置。
- 不内置任何 API Key、Token、代理 IP 或个人敏感信息。

## 安装

```bash
git clone https://github.com/haohaoi34/GPU-Hunter.git
cd GPU-Hunter
python3 --version
```

本项目仅使用 Python 标准库，不需要额外安装依赖。

## 全新 Ubuntu 一键运行

在一台全新的 Ubuntu 服务器里，复制下面命令即可自动安装基础环境、克隆仓库并启动交互式运行：

```bash
sudo apt-get update && sudo apt-get install -y curl ca-certificates && bash <(curl -fsSL https://raw.githubusercontent.com/haohaoi34/GPU-Hunter/main/run-ubuntu.sh)
```

脚本默认会把项目放到：

```text
~/GPU-Hunter
```

如果你想指定安装目录：

```bash
sudo apt-get update && sudo apt-get install -y curl ca-certificates && GPU_HUNTER_DIR="$HOME/apps/GPU-Hunter" bash <(curl -fsSL https://raw.githubusercontent.com/haohaoi34/GPU-Hunter/main/run-ubuntu.sh)
```

也可以把参数直接传给程序，例如先测试一轮不下单：

```bash
sudo apt-get update && sudo apt-get install -y curl ca-certificates && bash <(curl -fsSL https://raw.githubusercontent.com/haohaoi34/GPU-Hunter/main/run-ubuntu.sh) --dry-run --once
```

## 交互运行

直接运行脚本后，按提示填写 API Key、价格、Telegram 信息和代理。

```bash
python3 gpu_hunter_server.py
```

启动时会依次询问：

- Vast API Key，必填。
- Clore API Key，必填。
- Telegram Bot Token，可直接回车跳过。
- Telegram Chat ID，可直接回车跳过。
- Vast Jupyter Token，可直接回车自动生成。
- Clore SSH / Jupyter 密码，可直接回车自动生成。
- RTX 5090 单卡最高价格，必填。
- RTX 4090 单卡最高价格，必填。
- 代理 IP，可逐行粘贴，空行结束。

代理支持以下格式：

```text
http://user:pass@ip:port/
ip:port:user:pass
ip:port
```

## 非交互运行

服务器或后台运行时，可以通过命令行参数传入配置：

```bash
python3 gpu_hunter_server.py \
  --no-prompt \
  --vast-api-key "YOUR_VAST_API_KEY" \
  --clore-api-key "YOUR_CLORE_API_KEY" \
  --price-5090 0.7 \
  --price-4090 0.4
```

也可以使用环境变量：

```bash
export VAST_API_KEY="YOUR_VAST_API_KEY"
export CLORE_API_KEY="YOUR_CLORE_API_KEY"
export PRICE_5090="0.7"
export PRICE_4090="0.4"

python3 gpu_hunter_server.py --no-prompt
```

## 常用参数

```bash
python3 gpu_hunter_server.py --help
```

| 参数 | 说明 |
| --- | --- |
| `--interval` | 扫描间隔，默认 `0.1` 秒。 |
| `--refresh` | 终端面板刷新间隔，默认 `1` 秒。 |
| `--vast-workers` | Vast 并发查询数，默认 `8`。 |
| `--dry-run` | 只查询和通知，不真实下单。 |
| `--once` | 每个平台只跑一轮，适合测试。 |
| `--no-proxy` | 不使用代理。 |
| `--proxy` | 手动传入代理，可重复使用。 |
| `--proxy-file` | 从文件读取代理，每行一个。 |
| `--telegram-bot-token` | Telegram Bot Token，可选。 |
| `--telegram-chat-id` | Telegram Chat ID，可选。 |

## 测试

建议第一次先 dry-run，确认 API Key、价格和代理配置没问题：

```bash
python3 gpu_hunter_server.py \
  --dry-run \
  --once \
  --vast-api-key "YOUR_VAST_API_KEY" \
  --clore-api-key "YOUR_CLORE_API_KEY" \
  --price-5090 0.7 \
  --price-4090 0.4
```

## 安全提醒

- 不要把自己的 API Key、Telegram Token、代理账号提交到 GitHub。
- 如果你用命令行传入敏感信息，服务器 shell history 里可能会留下记录。
- 更推荐用环境变量、`.env`、systemd 环境文件或密钥管理工具保存敏感配置。
- 真实运行前请先用 `--dry-run --once` 测试。

## 免责声明

本项目只负责按你提供的配置自动查询和尝试下单。实际租用费用、平台规则、API 限制、账号风控和机器可用性由对应平台决定。请确认价格上限、下单数量和账号余额后再运行。
