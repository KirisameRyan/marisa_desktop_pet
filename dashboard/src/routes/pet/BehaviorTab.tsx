import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Eye, Palette, RefreshCw, Save, Sparkles, Timer } from 'lucide-react'
import {
  fetchPetConfig,
  updatePetConfig,
  type PetBehaviorConfig,
  type PetConfigBundle
} from '@/lib/pet-api'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Slider } from '@/components/ui/slider'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from '@/components/ui/select'

const SCALE_OPTIONS = [
  { value: '0.8', label: '0.8x（精致小巧）' },
  { value: '1', label: '1.0x（原生默认）' },
  { value: '1.25', label: '1.25x（稍大更清晰）' },
  { value: '1.5', label: '1.5x（元气巨萌）' }
]

const HOTKEY_OPTIONS = [
  { value: 'ctrl+alt+m', label: 'Ctrl+Alt+M' },
  { value: 'ctrl+alt+space', label: 'Ctrl+Alt+Space' },
  { value: 'ctrl+shift+space', label: 'Ctrl+Shift+Space' },
  { value: 'ctrl+alt+z', label: 'Ctrl+Alt+Z' }
]

/** 桌宠行为：巡检、外观尺寸、互动陪伴与音乐播报的开关。 */
export function BehaviorTab() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<PetBehaviorConfig>({})
  const [savedAt, setSavedAt] = useState<string | null>(null)

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['pet-config'],
    queryFn: fetchPetConfig
  })

  useEffect(() => {
    if (data?.pet) {
      setDraft(data.pet)
    }
  }, [data])

  const saveMutation = useMutation({
    mutationFn: (patch: Partial<PetConfigBundle>) => updatePetConfig(patch),
    onSuccess: (bundle) => {
      setDraft(bundle.pet)
      setSavedAt(new Date().toLocaleTimeString())
      queryClient.invalidateQueries({ queryKey: ['pet-config'] })
    }
  })

  const patch = (partial: PetBehaviorConfig) => setDraft((prev) => ({ ...prev, ...partial }))
  const inspectMinutes = Math.max(2, Math.round((draft.inspect_interval_seconds ?? 600) / 60))
  const presenceMinutes = Math.max(1, Math.round((draft.presence_idle_seconds ?? 300) / 60))

  if (isLoading) {
    return <div className="text-sm text-muted-foreground p-6">正在读取桌宠配置…</div>
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          这里的改动会直接写入桌宠配置，桌宠会在数秒内自动生效，无需重启。
        </p>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => refetch()} className="gap-1.5">
            <RefreshCw className="w-4 h-4" />
            重新读取
          </Button>
          <Button
            size="sm"
            className="gap-1.5"
            disabled={saveMutation.isPending}
            onClick={() => saveMutation.mutate({ pet: draft })}
          >
            <Save className="w-4 h-4" />
            保存并应用
          </Button>
        </div>
      </div>

      {saveMutation.isError && (
        <div className="p-3 rounded-md border border-destructive/40 bg-destructive/10 text-sm text-destructive">
          保存失败：{(saveMutation.error as Error)?.message || '未知异常'}
        </div>
      )}
      {savedAt && !saveMutation.isPending && !saveMutation.isError && (
        <div className="p-3 rounded-md border border-emerald-500/30 bg-emerald-500/10 text-sm text-emerald-600">
          已保存并于 {savedAt} 生效
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card className="shadow-sm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Eye className="w-5 h-5 text-sky-500" />
              桌面定时巡检
            </CardTitle>
            <CardDescription>
              魔理沙每隔一段时间偷偷看一眼屏幕，主动找主人搭话（深夜自动免打扰）。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="flex items-center justify-between">
              <Label htmlFor="auto-inspect">开启定时巡检</Label>
              <Switch
                id="auto-inspect"
                checked={draft.auto_screen_inspect ?? true}
                onCheckedChange={(checked) => patch({ auto_screen_inspect: checked })}
              />
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <span className="flex items-center gap-1.5">
                  <Timer className="w-4 h-4 text-muted-foreground" />
                  巡检间隔
                </span>
                <span className="text-muted-foreground">{inspectMinutes} 分钟</span>
              </div>
              <Slider
                value={[inspectMinutes]}
                min={2}
                max={60}
                step={1}
                disabled={!(draft.auto_screen_inspect ?? true)}
                onValueChange={([value]) => patch({ inspect_interval_seconds: value * 60 })}
              />
            </div>
          </CardContent>
        </Card>

        <Card className="shadow-sm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Palette className="w-5 h-5 text-amber-500" />
              外观尺寸
            </CardTitle>
            <CardDescription>调整桌面上魔理沙的大小，保存后立即缩放。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="space-y-2">
              <Label>缩放倍率</Label>
              <Select
                value={String(draft.scale ?? 1)}
                onValueChange={(value) => patch({ scale: Number(value) })}
              >
                <SelectTrigger>
                  <SelectValue placeholder="选择缩放倍率" />
                </SelectTrigger>
                <SelectContent>
                  {SCALE_OPTIONS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </CardContent>
        </Card>

        <Card className="shadow-sm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Sparkles className="w-5 h-5 text-violet-500" />
              互动与陪伴
            </CardTitle>
            <CardDescription>离开检测、拖拽送回收站、全局热键与整活行为。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="flex items-center justify-between">
              <Label htmlFor="presence-watch">主人离开时打瞌睡，回来打招呼</Label>
              <Switch
                id="presence-watch"
                checked={draft.presence_watch ?? true}
                onCheckedChange={(checked) => patch({ presence_watch: checked })}
              />
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <span>判定离开的时长</span>
                <span className="text-muted-foreground">{presenceMinutes} 分钟</span>
              </div>
              <Slider
                value={[presenceMinutes]}
                min={1}
                max={30}
                step={1}
                disabled={!(draft.presence_watch ?? true)}
                onValueChange={([value]) => patch({ presence_idle_seconds: value * 60 })}
              />
            </div>
            <div className="flex items-center justify-between">
              <Label htmlFor="magic-delete">允许拖文件到魔理沙身上送进回收站</Label>
              <Switch
                id="magic-delete"
                checked={draft.magic_delete ?? true}
                onCheckedChange={(checked) => patch({ magic_delete: checked })}
              />
            </div>
            <div className="flex items-center justify-between gap-4">
              <Label htmlFor="hotkey-enable">全局呼叫热键</Label>
              <div className="flex items-center gap-2">
                <Select
                  value={draft.hotkey_combo ?? 'ctrl+alt+m'}
                  onValueChange={(value) => patch({ hotkey_combo: value })}
                  disabled={!(draft.hotkey_enable ?? true)}
                >
                  <SelectTrigger className="w-[190px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {HOTKEY_OPTIONS.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Switch
                  id="hotkey-enable"
                  checked={draft.hotkey_enable ?? true}
                  onCheckedChange={(checked) => patch({ hotkey_enable: checked })}
                />
              </div>
            </div>
            <div className="flex items-center justify-between">
              <Label htmlFor="zany-auto">整活行为自动触发（跑/躺/吊窗等）</Label>
              <Switch
                id="zany-auto"
                checked={draft.zany_auto ?? true}
                onCheckedChange={(checked) => patch({ zany_auto: checked })}
              />
            </div>
            <div className="space-y-2">
              <Label>整活频率</Label>
              <Select
                value={String(draft.zany_level ?? 1)}
                onValueChange={(value) => patch({ zany_level: Number(value) })}
                disabled={!(draft.zany_auto ?? true)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1">克制（安静陪伴为主）</SelectItem>
                  <SelectItem value="2">热闹（偶尔到处皮）</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </CardContent>
        </Card>

        <Card className="shadow-sm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Timer className="w-5 h-5 text-rose-500" />
              音乐播报
            </CardTitle>
            <CardDescription>切歌播报、整点报时与每日推荐歌单。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="flex items-center justify-between">
              <Label htmlFor="song-notify">切歌时播报（气泡 + 报歌名）</Label>
              <Switch
                id="song-notify"
                checked={draft.song_notify ?? true}
                onCheckedChange={(checked) => patch({ song_notify: checked })}
              />
            </div>
            <div className="flex items-center justify-between">
              <Label htmlFor="hourly-chime">整点报时</Label>
              <Switch
                id="hourly-chime"
                checked={draft.hourly_chime ?? false}
                onCheckedChange={(checked) => patch({ hourly_chime: checked })}
              />
            </div>
            <div className="flex items-center justify-between">
              <Label htmlFor="marisa-daily">每天更新魔理推荐歌单并提醒</Label>
              <Switch
                id="marisa-daily"
                checked={draft.marisa_daily_notify ?? true}
                onCheckedChange={(checked) => patch({ marisa_daily_notify: checked })}
              />
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
