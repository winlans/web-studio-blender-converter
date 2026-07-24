# Runpod Serverless 部署

该目录提供队列型 Runpod Serverless worker。任务输入固定为 GLB，输出固定为符合
`BLENDER-HEADLESS-FALLBACK.md` 合同的通用 GLB。

## 构建

```bash
cd web-studio
docker build \
  -f scripts/blender-converter/Dockerfile \
  -t <registry>/cysta-model-converter:1.0.0 \
  .
docker push <registry>/cysta-model-converter:1.0.0
```

镜像固定使用 Blender 5.0.1。发布镜像时应保存镜像 digest，并在 Runpod endpoint
中使用 digest，而不是浮动 tag。

## Runpod endpoint

创建 Queue-based Serverless endpoint，容器并发设为 1。单 worker 建议至少 4 CPU、
8 GiB 内存和 6 GiB container disk。入口由 `runpod.serverless.start()` 提供，不需要
暴露容器端口。

从 GitHub 创建 endpoint 时选择包含本目录的分支，Dockerfile Path 填：

```text
/scripts/blender-converter/Dockerfile
```

Runpod 当前可能仍按默认分支 `main` 做 handler 静态检查。如果代码仅在其他分支，
页面会显示 “Could not find runpod.serverless.start()”，这不会阻止所选分支的构建。
实际 worker 入口是 `runpod_entrypoint.py`。

必需环境变量：

| 变量 | 说明 |
| --- | --- |
| `S3_BUCKET` | 与 cysta-ai 相同的 bucket |
| `S3_REGION` | S3 region |
| `S3_ACCESS_KEY_ID` | 仅具备目标 bucket 所需权限的 access key |
| `S3_SECRET_ACCESS_KEY` | 对应 secret |
| `S3_PUBLIC_BASE_URL` | 输出文件的 CDN/S3 公网前缀 |

可选环境变量：

| 变量 | 默认值 |
| --- | --- |
| `S3_ENDPOINT` | AWS SDK 默认 endpoint |
| `MODEL_CONVERT_OUTPUT_PREFIX` | `model-convert/output` |
| `MODEL_CONVERT_MAX_INPUT_BYTES` | `536870912` |
| `MODEL_CONVERT_DOWNLOAD_TIMEOUT_SECONDS` | `120` |
| `BLENDER_TIMEOUT_SECONDS` | `300` |
| `BLENDER_TEXTURE_RESOLUTION` | `2048` |

不要把凭据写入镜像或仓库。

## 请求与结果

提交给 Runpod `/run` 的 input：

```json
{
  "input": {
    "task_id": "123456789",
    "source_url": "https://cdn.example.com/input.glb",
    "output_key": "model-convert/output/123456789.glb"
  }
}
```

worker 成功结果：

```json
{
  "task_id": "123456789",
  "output_url": "https://cdn.example.com/model-convert/output/123456789.glb",
  "output_key": "model-convert/output/123456789.glb",
  "output_size": 123456,
  "reused": false
}
```

## cysta-ai 接入

生产环境至少配置：

```bash
CYSTA_MODEL_CONVERT_ENABLED=true
RUNPOD_MODEL_CONVERT_ENDPOINT_ID=<endpoint-id>
RUNPOD_MODEL_CONVERT_API_KEY=<api-key>
```

`cysta-ai` 提供两个需登录接口：

```http
POST /api/v1/model-convert-tasks
Content-Type: application/json

{"sourceUrl":"https://cdn.example.com/input.glb?signature=temporary"}
```

```http
GET /api/v1/model-convert-tasks/{taskId}
```

接口固定执行 GLB 到通用 GLB 的转换，因此没有 format 或 profile 参数。相同用户、
相同源 URL 和相同转换器版本只会对应一个 `crysta_model_convert_task`。任务表只保存
输入与输出 URL，不保存对象 key 或其 hash。`cysta-ai` 不对输入对象执行 HEAD、类型
或大小检查，只将原始 `sourceUrl` 转发给 Runpod。Worker 成功后，查询接口的
`outputUrl` 才会有值。

任务从创建起保留 24 小时。到期后后台任务会取消仍在执行的 Runpod job，并直接按
对象 key 删除输入与输出，不写入 `sys_oss`，清理失败会继续重试。

## 本地验证

```bash
python -m unittest test_handler.py

blender --background --factory-startup --disable-autoexec \
  --python blender_standardize_glb.py -- \
  input.glb /tmp/output.glb \
  --resolution 64 --margin 4 --samples 1 \
  --uv preserve --apply-modifiers --bake-mode color
```
