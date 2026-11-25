# 门户静态资源

设计范式文件复制自鹿溪设计范式 `LUCY-DESIGN`（2026-11-16），目的是让本仓库
可以独立安装与运行，不再依赖仓库外目录：

- `tokens.css`：LUXI Design System tokens；
- `qilin.svg`：栖林产品方标（页面头部与详情页使用）；
- `luxi-lab-gold.svg`：实验室烫金符号标。

图标（favicon）与 `qilin.svg` 同源——同一套绿色底、白色与金色双人字、蓝色基线，
只是按 16–32px 的光学需要加粗并去掉小装饰：

- `favicon.svg`：矢量 favicon（现代浏览器）；
- `favicon.ico`：16/32/48 三档保底，服务在 `/favicon.ico`；
- `apple-touch-icon.png`：180×180 满底色，iOS 加到主屏用。

重新生成（改图形时三份一起换）：

```sh
rsvg-convert -w 16 -h 16 favicon.svg -o /tmp/f16.png
rsvg-convert -w 32 -h 32 favicon.svg -o /tmp/f32.png
rsvg-convert -w 48 -h 48 favicon.svg -o /tmp/f48.png
python3 - <<'PY'
from PIL import Image
imgs = [Image.open(f"/tmp/f{s}.png").convert("RGBA") for s in (16, 32, 48)]
imgs[-1].save("favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
PY
# apple-touch-icon 用无圆角版本渲染 180×180，避免 iOS 蒙版下出现透明角
```

`tokens.css` 里的 `--luxi-green` 等变量名被门户与测试引用。更新设计范式时，
从 `LUCY-DESIGN` 重新复制 `tokens.css` / `qilin.svg` / `luxi-lab-gold.svg`，
不要在这里单独改样式。
