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
