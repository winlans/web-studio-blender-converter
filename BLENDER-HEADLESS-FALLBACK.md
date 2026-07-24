# Blender Headless 模型转换后端部署说明（旧方案参考）

> 当前生产方案改为 Runpod Queue-based Serverless Worker，部署和接口说明见
> `RUNPOD-SERVERLESS.md`。本文仅保留 Blender 脚本与裸机运行背景，不再作为
> `cysta-ai` 接口合同。

## 1. 用途

该服务在服务器端调用 Blender Headless，将 `.glb`、`.gltf` 或 `.obj`
标准化为通用 GLB。

输出文件应包含：

- 三角形 Mesh；
- `POSITION`、`NORMAL`、`TEXCOORD_0` 和 indices；
- 可应用的 modifier；
- 烘焙后的 base color texture；
- 内嵌或可独立分发的纹理资源。

项目已经提供 Blender 转换脚本：

```text
tools/blender_standardize_glb.py
```

该脚本必须由 Blender 自带的 Python 运行，不能使用系统 Python 直接执行。

## 2. 运行环境

建议配置：

| 项目 | 建议值 |
| --- | --- |
| Java | 21 |
| Spring Boot | 4.x 或公司批准版本 |
| Blender | 固定的生产版本 |
| CPU | 每个并发任务至少 2 核 |
| 内存 | 每个并发任务建议 4–8 GB |
| 临时磁盘 | 至少为最大上传文件的 3–5 倍 |
| 操作系统 | Linux x86_64 |

不要在生产环境使用浮动的 Blender daily build。API 服务、Blender 版本和
转换脚本版本应一起锁定。

## 3. Blender Headless 裸机部署

### 3.1 安装 Blender

使用系统包管理器安装：

```bash
sudo apt-get update
sudo apt-get install -y blender
```

生产环境更推荐下载并固定 Blender 官方二进制目录，例如：

```text
/opt/blender/blender
```

确认版本：

```bash
/opt/blender/blender --version
```

### 3.2 部署转换脚本

```bash
sudo mkdir -p /opt/blender-converter/tools
sudo cp tools/blender_standardize_glb.py \
  /opt/blender-converter/tools/blender_standardize_glb.py
```

建议记录脚本 SHA-256：

```bash
sha256sum /opt/blender-converter/tools/blender_standardize_glb.py
```

### 3.3 验证 Headless 转换

```bash
/opt/blender/blender \
  --background \
  --factory-startup \
  --python /opt/blender-converter/tools/blender_standardize_glb.py \
  -- \
  /data/input.glb \
  /data/output.glb \
  --resolution 2048 \
  --margin 16 \
  --samples 1 \
  --uv preserve \
  --apply-modifiers \
  --bake-mode color
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `--background` | 无界面运行 Blender |
| `--factory-startup` | 不加载用户配置和插件 |
| `--resolution` | 烘焙纹理分辨率 |
| `--margin` | UV 岛烘焙边距 |
| `--samples` | Cycles 烘焙采样数 |
| `--uv preserve` | 保留有效 UV；没有 UV 时自动展开 |
| `--apply-modifiers` | 应用可执行的 modifier |
| `--bake-mode color` | 烘焙材质颜色，不引入场景灯光 |

验证输出：

```bash
test -s /data/output.glb
head -c 4 /data/output.glb
```

第二条命令应输出：

```text
glTF
```

## 4. Java 后端接口

推荐接口：

```http
POST /api/blender/convert
Content-Type: multipart/form-data
```

表单字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `file` | 是 | `.glb`、`.gltf` 或 `.obj` |
| `output_format` | 否 | 默认 `glb` |
| `profile` | 否 | 默认 `mesh-uv-texture` |

如果后端负责上传对象存储，推荐返回：

```json
{
  "requestId": "5ec8b9dd-b0b6-4b26-a042-d74dc9c5ca28",
  "cdnUrl": "https://cdn.example.com/models/5ec8b9dd/model.glb"
}
```

错误响应示例：

```json
{
  "requestId": "5ec8b9dd-b0b6-4b26-a042-d74dc9c5ca28",
  "code": "BLENDER_CONVERSION_FAILED",
  "message": "Model conversion failed"
}
```

建议状态码：

| 状态码 | 场景 |
| --- | --- |
| `400` | 参数错误 |
| `413` | 文件过大 |
| `415` | 不支持的格式 |
| `422` | Blender 无法导入或转换 |
| `429` | 转换队列已满 |
| `500` | 输出文件无效 |
| `504` | Blender 执行超时 |

## 5. Spring Boot 配置

`application.yml`：

```yaml
server:
  port: 8080
  tomcat:
    connection-timeout: 10s

spring:
  servlet:
    multipart:
      max-file-size: 512MB
      max-request-size: 512MB

management:
  endpoints:
    web:
      exposure:
        include: health,info,prometheus

converter:
  blender-bin: ${BLENDER_BIN:/opt/blender/blender}
  script: ${BLENDER_SCRIPT:/opt/blender-converter/tools/blender_standardize_glb.py}
  timeout: ${CONVERT_TIMEOUT:300s}
  concurrency: ${BLENDER_CONCURRENCY:2}
  max-upload-bytes: ${MAX_UPLOAD_BYTES:536870912}
  texture-resolution: ${BAKE_RESOLUTION:2048}
  work-root: ${CONVERT_WORK_ROOT:/tmp/blender-converter}
```

## 6. Java 调用 Blender

核心实现应完成：

1. 校验扩展名和上传大小；
2. 为每个任务创建独立临时目录；
3. 把上传文件写入临时目录；
4. 使用 `ProcessBuilder` 启动 Blender；
5. 将 stdout/stderr 重定向到日志文件；
6. 限制并发并设置超时；
7. 校验输出 GLB；
8. 上传对象存储；
9. 在 `finally` 中清理临时目录。

示例：

```java
public Path convert(Path input, Path workDir, Duration timeout)
        throws IOException, InterruptedException {

    Path output = workDir.resolve("output.glb");
    Path log = workDir.resolve("blender.log");

    List<String> command = List.of(
            blenderBin,
            "--background",
            "--factory-startup",
            "--python", blenderScript,
            "--",
            input.toString(),
            output.toString(),
            "--resolution", Integer.toString(textureResolution),
            "--margin", "16",
            "--samples", "1",
            "--uv", "preserve",
            "--apply-modifiers",
            "--bake-mode", "color"
    );

    Process process = new ProcessBuilder(command)
            .directory(workDir.toFile())
            .redirectErrorStream(true)
            .redirectOutput(log.toFile())
            .start();

    if (!process.waitFor(timeout.toMillis(), TimeUnit.MILLISECONDS)) {
        process.destroy();
        if (!process.waitFor(5, TimeUnit.SECONDS)) {
            process.destroyForcibly();
            process.waitFor();
        }
        throw new ConversionTimeoutException();
    }

    if (process.exitValue() != 0 || !Files.isRegularFile(output)) {
        throw new BlenderConversionException(readLogTail(log));
    }

    validateGlb(output);
    return output;
}
```

GLB 校验：

```java
private static void validateGlb(Path output) throws IOException {
    if (Files.size(output) < 12) {
        throw new IOException("Invalid GLB output");
    }
    try (InputStream input = Files.newInputStream(output)) {
        byte[] magic = input.readNBytes(4);
        if (!Arrays.equals(magic, new byte[]{'g', 'l', 'T', 'F'})) {
            throw new IOException("Invalid GLB magic");
        }
    }
}
```

不要把完整 Blender 日志返回给公网调用方。响应只返回错误码、requestId 和简短
消息，完整日志写入内部日志系统。

## 7. Docker 部署

### 7.1 Dockerfile

```dockerfile
FROM maven:3-eclipse-temurin-21 AS build
WORKDIR /source
COPY pom.xml .
COPY src ./src
RUN mvn -B -DskipTests package

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       blender \
       ca-certificates \
       openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 converter
WORKDIR /app

COPY --from=build /source/target/blender-converter-*.jar /app/app.jar
COPY tools/blender_standardize_glb.py /app/tools/blender_standardize_glb.py

RUN chown -R converter:converter /app
USER converter

ENV BLENDER_BIN=/usr/bin/blender
ENV BLENDER_SCRIPT=/app/tools/blender_standardize_glb.py
ENV BLENDER_CONCURRENCY=2
ENV CONVERT_TIMEOUT=300s
ENV MAX_UPLOAD_BYTES=536870912
ENV CONVERT_WORK_ROOT=/tmp/blender-converter

EXPOSE 8080
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=60", "-jar", "/app/app.jar"]
```

### 7.2 构建镜像

```bash
docker build -t blender-converter:1.0.0 .
```

### 7.3 启动容器

```bash
docker run -d \
  --name blender-converter \
  --restart unless-stopped \
  -p 8080:8080 \
  --memory=8g \
  --cpus=4 \
  --pids-limit=256 \
  --read-only \
  --tmpfs /tmp:size=6g,mode=1777 \
  blender-converter:1.0.0
```

### 7.4 健康检查

```bash
curl http://127.0.0.1:8080/actuator/health
```

预期：

```json
{"status":"UP"}
```

转换测试：

```bash
curl -f \
  -F "file=@test.glb" \
  -F "output_format=glb" \
  -F "profile=mesh-uv-texture" \
  http://127.0.0.1:8080/api/blender/convert
```

查看日志：

```bash
docker logs -f blender-converter
```

## 8. Nginx 配置

```nginx
location /api/blender/ {
    proxy_pass http://blender-converter:8080/api/blender/;
    proxy_request_buffering on;
    proxy_connect_timeout 10s;
    proxy_send_timeout 60s;
    proxy_read_timeout 360s;
    client_max_body_size 512m;
}
```

如果使用异步任务接口，Nginx 不需要保持数分钟的同步请求，生产环境更推荐异步模式。

## 9. OBJ、MTL 和外部资源

单个 OBJ 文件可能引用 MTL 和外部纹理，单文件上传不能保证材质完整。建议支持 ZIP
任务包：

```text
model-package.zip
├── model.obj
├── model.mtl
└── textures/
    ├── basecolor.png
    └── normal.png
```

后端解压时必须：

- 拒绝绝对路径和 `..`；
- 校验 `target.normalize().startsWith(workDir)`；
- 拒绝符号链接；
- 限制文件数量、单文件大小和解压总量；
- 防止 ZIP Bomb；
- 只允许白名单扩展名。

glTF 的外部 `.bin` 和图片也建议通过相同的任务包上传。

## 10. 生产环境建议

对于大文件或高并发场景，建议使用异步任务：

```text
API → 对象存储 → 消息队列 → Blender Worker → 对象存储/CDN
```

接口可以设计为：

```http
POST   /api/blender/jobs
GET    /api/blender/jobs/{id}
GET    /api/blender/jobs/{id}/result
DELETE /api/blender/jobs/{id}
```

任务状态：

```text
QUEUED
RUNNING
SUCCEEDED
FAILED
CANCELLED
EXPIRED
```

建议使用以下内容作为缓存键：

```text
SHA-256(
  source file
  + Blender version
  + conversion script version
  + conversion parameters
)
```

## 11. 安全与运维检查

- Blender 与 Java 服务使用非 root 用户运行。
- 每个任务使用独立临时目录和 `--factory-startup`。
- 限制 CPU、内存、PID、临时磁盘和并发任务数。
- 不挂载宿主机敏感目录、Docker Socket 或云平台凭据。
- Blender Worker 与公网入口尽量分开部署。
- 对上传文件执行类型、扩展名和大小校验。
- 所有成功、失败、取消和超时路径都清理临时文件。
- 定期清理异常退出遗留目录和过期对象存储文件。
- 监控队列长度、转换耗时、失败率、超时率、内存和临时磁盘。
- 对 Blender、Java 基础镜像和依赖执行漏洞扫描。

## 12. 部署验收

1. `blender --version` 返回固定版本。
2. Blender 命令行可以独立生成合法 GLB。
3. Java 健康检查返回 `UP`。
4. API 拒绝不支持的格式和超大文件。
5. 转换超时后 Blender 子进程被终止。
6. 输出 GLB 的 magic 为 `glTF`。
7. 输出文件成功上传对象存储并能通过 CDN 下载。
8. 并发达到上限时任务排队或返回 `429`。
9. 请求完成后临时目录被清理。
10. 容器重启后没有遗留孤儿 Blender 进程。
