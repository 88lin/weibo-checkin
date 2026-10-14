# weibo-checkin 🐴

微博超话每日自动签到 + 自动评论。纯 Python 标准库（零依赖），网页版接口，无需账号密码。

## 工作原理

1. 从 [s.weibo.com](https://s.weibo.com) 搜索结果里解析出超话的 containerid（`100808…`）
2. 调用网页版签到接口 `POST /ajax_proxy/chaohua/page/checkin`
3. 拉取超话最新帖子，随机选一条语料调用 `POST /ajax/comments/create` 发评论
4. 请求间随机间隔 5~12 秒，降低风控风险

认证只靠 Cookie（含 `XSRF-TOKEN`，会自动作为 `X-Xsrf-Token` 头回传）。

## 使用

```bash
git clone https://github.com/88lin/weibo-checkin.git
cd weibo-checkin
cp config.example.json config.json
```

编辑 `config.json`：

- `cookie`：浏览器登录 weibo.com 后，F12 → Network → 任意请求 → 复制请求头里的完整 `Cookie` 值
- `supertopics`：要签到的超话名列表，如 `["数码", "小米"]`
- `comments`：评论语料池，每次随机抽一条；留空 `[]` 则只签到不评论

运行：

```bash
python3 weibo_checkin.py run               # 签到 + 评论
python3 weibo_checkin.py run --no-comment  # 只签到
```

## 定时任务（crontab）

每天 9:30 自动执行：

```
30 9 * * * cd /path/to/weibo-checkin && /usr/bin/python3 weibo_checkin.py run >> cron.log 2>&1
```

## 注意

- ⚠️ `config.json` 含登录凭证，已在 `.gitignore` 中排除，**切勿手动提交或分享**
- Cookie 有效期通常数周~数月，过期后日志会出现"签到失败"，重新抓一次即可
- 评论语料建议自己改写，避免和别人撞车
- 仅供个人学习，请勿高频滥用（账号被限制概不负责）

## License

MIT
