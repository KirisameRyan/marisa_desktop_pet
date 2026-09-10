import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Music, RefreshCw, Save, ScanLine, Volume2 } from 'lucide-react'
import { Link } from '@tanstack/react-router'
import {
  fetchNcmStatus,
  fetchPetConfig,
  startNcmLogin,
  updatePetConfig,
  type PetVoiceConfig
} from '@/lib/pet-api'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'

/** 语音与音乐：本地 GPT-SoVITS 参数、网易云扫码登录。 */
export function VoiceTab() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<PetVoiceConfig>({})
  const [savedAt, setSavedAt] = useState<string | null>(null)
  const [qrcode, setQrcode] = useState<string | null>(null)

  const { data: config } = useQuery({ queryKey: ['pet-config'], queryFn: fetchPetConfig })
  const { data: ncmStatus, refetch: refetchNcm } = useQuery({
    queryKey: ['pet-ncm-status'],
    queryFn: fetchNcmStatus,
    refetchInterval: qrcode ? 3000 : false
  })

  useEffect(() => {
    if (config?.tts) {
      setDraft(config.tts)
    }
  }, [config])

  useEffect(() => {
    if (ncmStatus?.logged_in) {
      setQrcode(null)
    }
  }, [ncmStatus?.logged_in])

  const saveMutation = useMutation({
    mutationFn: (tts: PetVoiceConfig) => updatePetConfig({ tts }),
    onSuccess: (bundle) => {
      setDraft(bundle.tts)
      setSavedAt(new Date().toLocaleTimeString())
      queryClient.invalidateQueries({ queryKey: ['pet-config'] })
    }
  })

  const loginMutation = useMutation({
    mutationFn: () => startNcmLogin(),
    onSuccess: (data) => {
      setQrcode(data.image_base64)
      refetchNcm()
    }
  })

  const patch = (partial: PetVoiceConfig) => setDraft((prev) => ({ ...prev, ...partial }))

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card className="shadow-sm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Volume2 className="w-5 h-5 text-emerald-500" />
              本地语音合成（GPT-SoVITS）
            </CardTitle>
            <CardDescription>
              音色模型与参考音频属于「语音插件」的服务参数，统一在插件配置里维护；
              这里只保留桌宠播报本身需要的开关。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="flex items-center justify-between">
              <Label htmlFor="tts-autostart">服务未启动时自动拉起</Label>
              <Switch
                id="tts-autostart"
                checked={draft.auto_start_api ?? true}
                onCheckedChange={(checked) => patch({ auto_start_api: checked })}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="tts-speed">语速倍率（{Number(draft.speed_factor ?? 1).toFixed(2)}x）</Label>
              <Input
                id="tts-speed"
                type="number"
                step="0.05"
                min="0.5"
                max="2"
                value={draft.speed_factor ?? 1}
                onChange={(e) => patch({ speed_factor: Number(e.target.value) })}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="tts-url">服务地址</Label>
              <Input
                id="tts-url"
                value={draft.gpt_sovits_api_url ?? ''}
                placeholder="http://127.0.0.1:9880"
                onChange={(e) => patch({ gpt_sovits_api_url: e.target.value })}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="tts-ref">参考音频路径</Label>
              <Input
                id="tts-ref"
                value={draft.ref_audio_path ?? ''}
                placeholder="用于克隆音色的参考音频（建议 3-10 秒）"
                onChange={(e) => patch({ ref_audio_path: e.target.value })}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="tts-prompt">参考文本（须与音频内容完全一致）</Label>
              <Textarea
                id="tts-prompt"
                rows={3}
                value={draft.prompt_text ?? ''}
                onChange={(e) => patch({ prompt_text: e.target.value })}
              />
            </div>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                className="gap-1.5"
                disabled={saveMutation.isPending}
                onClick={() => saveMutation.mutate(draft)}
              >
                <Save className="w-4 h-4" />
                保存语音参数
              </Button>
              <Button variant="outline" size="sm" className="gap-1.5" asChild>
                <Link to="/plugin-config">
                  <ExternalLink className="w-4 h-4" />
                  打开插件配置（含音色服务）
                </Link>
              </Button>
              {savedAt && <span className="text-xs text-muted-foreground">已保存 {savedAt}</span>}
            </div>
          </CardContent>
        </Card>

        <Card className="shadow-sm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Music className="w-5 h-5 text-rose-500" />
              网易云音乐登录
            </CardTitle>
            <CardDescription>
              扫码登录后，桌面与 QQ 两侧都能点歌、拉取每日推荐与「我喜欢的音乐」。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center gap-2 text-sm">
              {ncmStatus?.logged_in ? (
                <span className="text-emerald-600">
                  已登录：{ncmStatus.nickname || '网易云用户'}
                </span>
              ) : (
                <span className="text-muted-foreground">尚未登录网易云</span>
              )}
              <Button variant="ghost" size="sm" onClick={() => refetchNcm()} className="gap-1 ml-auto">
                <RefreshCw className="w-3.5 h-3.5" />
                刷新
              </Button>
            </div>

            <Button
              size="sm"
              className="gap-1.5"
              disabled={loginMutation.isPending}
              onClick={() => loginMutation.mutate()}
            >
              <ScanLine className="w-4 h-4" />
              {ncmStatus?.logged_in ? '重新扫码登录' : '生成登录二维码'}
            </Button>

            {loginMutation.isError && (
              <p className="text-sm text-destructive">
                生成二维码失败：{(loginMutation.error as Error)?.message || '未知异常'}
              </p>
            )}

            {qrcode && (
              <div className="flex flex-col items-center gap-2 rounded-lg border p-3">
                <img src={qrcode} alt="网易云登录二维码" className="w-[190px] h-[190px]" />
                <p className="text-xs text-muted-foreground">
                  请用手机网易云 App 扫码，两分钟内有效；扫码成功后本卡片会自动更新状态。
                </p>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
