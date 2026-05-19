# 使用 Python 官方轻量镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目所有文件到容器
COPY . .

# 创建上传目录
RUN mkdir -p uploads

# Cloud Run 会通过环境变量 PORT 告诉你该监听哪个端口
ENV PORT=8080

# 用 gunicorn (生产级服务器) 启动 Flask 应用
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 app:app
