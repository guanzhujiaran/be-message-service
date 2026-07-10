FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /fastapi

# 配置 Debian 阿里云镜像源（加速 apt 安装）
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && \
    sed -i 's/security.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources

# 安装构建工具（部分 python 包可能需要从源码编译）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# 先拷贝依赖描述与 lock 文件，利用层缓存。
# uv sync 会使用 pyproject.toml 中配置的阿里云/腾讯云/清华 PyPI 镜像源。
COPY pyproject.toml ./
COPY uv.lock ./

RUN uv sync --no-dev

# 拷贝源码
COPY app app

# 健康检查 HTTP 服务端口（FastAPI 暴露 /health）
EXPOSE 18739

# 本服务是标准 FastAPI 应用，通过 uv 运行 uvicorn 启动；
# FastStream 的 RabbitMQ broker 作为 FastAPI 插件在 lifespan 中接入（启动即消费）
# 监听端口写死为 18739（对外由 MESSAGE_SERVICE_PORT 做端口转换/配置）
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18739"]
