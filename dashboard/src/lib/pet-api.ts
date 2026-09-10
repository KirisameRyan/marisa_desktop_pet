import { backendApi } from '@/lib/http'

export interface PetStatus {
  ws_online: boolean
  ws_port: number
  process_running: boolean
  uptime: string
  character: string
  personality: string
}

export interface PetScreenshot {
  image_base64: string
  timestamp: number
}

export async function fetchPetStatus(): Promise<PetStatus> {
  const res = await backendApi.get<{ status: string; data: PetStatus }>('/api/webui/pet/status')
  return res.data
}

export async function fetchPetScreenshot(): Promise<PetScreenshot> {
  const res = await backendApi.get<{ status: string; data: PetScreenshot }>('/api/webui/pet/screenshot')
  return res.data
}

export async function executePetAction(action: string, param: string = ''): Promise<string> {
  const res = await backendApi.post<{ status: string; message: string }>('/api/webui/pet/action', {
    body: {
      action,
      param,
    },
  })
  return res.message
}

export interface PetBehaviorConfig {
  auto_screen_inspect?: boolean
  inspect_interval_seconds?: number
  scale?: number
  song_notify?: boolean
  hourly_chime?: boolean
  marisa_daily_notify?: boolean
  presence_watch?: boolean
  presence_idle_seconds?: number
  magic_delete?: boolean
  hotkey_enable?: boolean
  hotkey_combo?: string
  zany_auto?: boolean
  zany_level?: number
}

export interface PetVoiceConfig {
  gpt_sovits_api_url?: string
  ref_audio_path?: string
  prompt_text?: string
  speed_factor?: number
  auto_start_api?: boolean
}

export interface PetConfigBundle {
  pet: PetBehaviorConfig
  tts: PetVoiceConfig
}

export async function fetchPetConfig(): Promise<PetConfigBundle> {
  const res = await backendApi.get<{ status: string; data: PetConfigBundle }>('/api/webui/pet/config')
  return res.data
}

export async function updatePetConfig(patch: {
  pet?: PetBehaviorConfig
  tts?: PetVoiceConfig
}): Promise<PetConfigBundle> {
  const res = await backendApi.put<{ status: string; data: PetConfigBundle }>('/api/webui/pet/config', {
    body: {
      pet: patch.pet ?? {},
      tts: patch.tts ?? {},
    },
  })
  return res.data
}

export interface PetFavorability {
  favorability: number
  total_interactions: number
  rank: string
  updated_at: string
  available: boolean
}

export async function fetchPetFavorability(): Promise<PetFavorability> {
  const res = await backendApi.get<{ status: string; data: PetFavorability }>('/api/webui/pet/favorability')
  return res.data
}

export interface NcmStatus {
  logged_in: boolean
  nickname: string
  has_qrcode: boolean
  qrcode_updated_at: number
}

export async function fetchNcmStatus(): Promise<NcmStatus> {
  const res = await backendApi.get<{ status: string; data: NcmStatus }>('/api/webui/pet/ncm/status')
  return res.data
}

export async function startNcmLogin(): Promise<{ image_base64: string; tip: string }> {
  const res = await backendApi.post<{ status: string; data: { image_base64: string; tip: string } }>(
    '/api/webui/pet/ncm/login'
  )
  return res.data
}
