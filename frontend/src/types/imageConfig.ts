export interface ImageModelConfig {
  image_enabled: boolean;
  image_api_key_enc?: string;  // 用户裁决(2026-11-13)：GET 不回传，可选
  image_base_url: string;
  image_model: string;
  image_default_size: string;
  image_price_per_image: number;
  image_provider: string;
  image_max_concurrency: number;
  max_ai_images: number;
  has_key?: boolean;
  api_key?: string;
}
export interface ImageConfigPreset { provider: string; base_url: string; model: string }
export interface ImageDecisionInfo {
  routes: { route: string; desc: string }[];
  decision_fn: string;
  prompt_template: string;
  prompt_fields: string[];
}
export interface ImageConfigResponse {
  config: ImageModelConfig;
  presets: Record<string, ImageConfigPreset>;
  decision: ImageDecisionInfo;
}
export interface ImageTestResult { ok: boolean; message: string }
// ✅ N1 契约修复（2026-11-14 · SDD Task 6 审查）：PUT /api/v1/image-config 载荷必须用
//    **裸字段**与后端 ImageConfigIn（backend/app/routers/image_config.py:37-46）对齐 ——
//    image_* 前缀字段（ImageModelConfig）是 GET 响应形态，不能用作保存载荷，
//    否则 Task 7 消费端按裸字段调用会 tsc 报错或静默保存默认配置。
export interface ImageConfigSavePayload {
  provider: string;
  model: string;
  base_url: string;
  api_key: string;
  enabled: boolean;
  default_size: string;
  concurrency: number;
  price_per_image: number;
  max_ai_images: number;
}
