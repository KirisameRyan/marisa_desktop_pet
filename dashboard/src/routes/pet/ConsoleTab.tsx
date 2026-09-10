import { useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import {
  Camera,
  CheckCircle2,
  XCircle,
  Clock,
  Heart,
  Bot,
  ExternalLink,
  Lock,
  Music,
  Play,
  RefreshCw,
  SkipBack,
  SkipForward,
  Sparkles,
  Terminal,
  Volume2,
  VolumeX
} from 'lucide-react'
import { fetchPetStatus, fetchPetScreenshot, executePetAction } from '@/lib/pet-api'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

/** 控制台：桌宠在线状态、多媒体直控、系统动作与屏幕快照。 */
export function ConsoleTab() {
  const [songName, setSongName] = useState('')
  const [appName, setAppName] = useState('')
  const [actionFeedback, setActionFeedback] = useState<string | null>(null)

  const { data: petStatus, isLoading: statusLoading, refetch: refetchStatus } = useQuery({
    queryKey: ['pet-status'],
    queryFn: fetchPetStatus,
    refetchInterval: 5000,
  })

  const { data: screenshot, isLoading: screenshotLoading, refetch: refetchScreenshot } = useQuery({
    queryKey: ['pet-screenshot'],
    queryFn: fetchPetScreenshot,
    enabled: false,
  })

  const actionMutation = useMutation({
    mutationFn: ({ action, param }: { action: string; param?: string }) =>
      executePetAction(action, param),
    onSuccess: (msg) => {
      setActionFeedback(msg)
      setTimeout(() => setActionFeedback(null), 4000)
    },
    onError: (err: any) => {
      setActionFeedback(`执行出错: ${err?.message || '未知异常'}`)
      setTimeout(() => setActionFeedback(null), 4000)
    },
  })

  const handleAction = (action: string, param: string = '') => {
    actionMutation.mutate({ action, param })
  }

  return (
    <div className="space-y-6">
      {actionFeedback && (
        <div className="p-3 bg-primary/10 border border-primary/20 rounded-md text-sm text-primary flex items-center gap-2 animate-in fade-in">
          <Sparkles className="w-4 h-4" />
          <span>{actionFeedback}</span>
        </div>
      )}

      <div className="flex justify-end">
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetchStatus()}
          disabled={statusLoading}
          className="flex items-center gap-1.5"
        >
          <RefreshCw className={`w-4 h-4 ${statusLoading ? 'animate-spin' : ''}`} />
          刷新状态
        </Button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card className="shadow-sm">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground flex items-center justify-between">
              桌宠网络网关
              <Bot className="w-4 h-4 text-blue-500" />
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-2">
              {petStatus?.ws_online ? (
                <>
                  <CheckCircle2 className="w-5 h-5 text-emerald-500" />
                  <span className="font-semibold text-lg text-emerald-600">在线就绪 (:8400)</span>
                </>
              ) : (
                <>
                  <XCircle className="w-5 h-5 text-destructive" />
                  <span className="font-semibold text-lg text-destructive">未连接 (:8400)</span>
                </>
              )}
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              本地 WebSocket 双工链路，桌面端与 QQ 端跨端共用大脑
            </p>
          </CardContent>
        </Card>

        <Card className="shadow-sm">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground flex items-center justify-between">
              宿主系统运行
              <Clock className="w-4 h-4 text-amber-500" />
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-semibold text-lg">{petStatus?.uptime || '检测中...'}</div>
            <p className="text-xs text-muted-foreground mt-1">已持续为主人守候电脑</p>
          </CardContent>
        </Card>

        <Card className="shadow-sm">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground flex items-center justify-between">
              当前人物与性格
              <Heart className="w-4 h-4 text-rose-500" />
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-semibold text-lg text-rose-600 dark:text-rose-400">
              {petStatus?.character || '雾雨魔理沙'}
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {petStatus?.personality || '普通的魔法使 (Ze!)'}
            </p>
          </CardContent>
        </Card>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="space-y-6">
          <Card className="shadow-sm">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Music className="w-5 h-5 text-rose-500" />
                网易云音乐与多媒体直控
              </CardTitle>
              <CardDescription>直接驱动本地网易云客户端播放与系统音量调节。</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex gap-2">
                <Input
                  placeholder="输入想听的歌名/歌手 (如 Bad Apple!!)..."
                  value={songName}
                  onChange={(e) => setSongName(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && songName && handleAction('play_music', songName)}
                />
                <Button
                  onClick={() => songName && handleAction('play_music', songName)}
                  disabled={!songName || actionMutation.isPending}
                >
                  搜索开播
                </Button>
              </div>

              <div className="flex gap-2 flex-wrap">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handleAction('play_recommend')}
                  disabled={actionMutation.isPending}
                >
                  <Sparkles className="w-3.5 h-3.5 mr-1 text-yellow-500" />
                  魔理今日推荐
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handleAction('play_my_playlist')}
                  disabled={actionMutation.isPending}
                >
                  <Heart className="w-3.5 h-3.5 mr-1 text-rose-500" />
                  我喜欢的音乐
                </Button>
              </div>

              <div className="flex items-center gap-2 pt-2 border-t flex-wrap">
                <Button
                  variant="outline"
                  size="icon"
                  onClick={() => handleAction('music_control', 'prev')}
                  title="上一首"
                >
                  <SkipBack className="w-4 h-4" />
                </Button>
                <Button
                  variant="default"
                  size="icon"
                  onClick={() => handleAction('music_control', 'toggle')}
                  title="播放 / 暂停"
                >
                  <Play className="w-4 h-4" />
                </Button>
                <Button
                  variant="outline"
                  size="icon"
                  onClick={() => handleAction('music_control', 'next')}
                  title="下一首"
                >
                  <SkipForward className="w-4 h-4" />
                </Button>

                <div className="h-6 w-px bg-border mx-2" />

                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handleAction('volume', 'down')}
                  className="flex items-center gap-1"
                >
                  音量 -
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handleAction('volume', 'up')}
                  className="flex items-center gap-1"
                >
                  <Volume2 className="w-4 h-4" />
                  音量 +
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handleAction('volume', 'mute')}
                  className="flex items-center gap-1"
                >
                  <VolumeX className="w-4 h-4" />
                  静音
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card className="shadow-sm">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Terminal className="w-5 h-5 text-indigo-500" />
                系统桌面快捷直控
              </CardTitle>
              <CardDescription>快速调用本地系统 API 执行常用管理动作。</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex gap-2">
                <Input
                  placeholder="启动软件名或网址 (如 VSCode / 记事本)..."
                  value={appName}
                  onChange={(e) => setAppName(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && appName && handleAction('open_app', appName)}
                />
                <Button
                  variant="secondary"
                  onClick={() => appName && handleAction('open_app', appName)}
                  disabled={!appName || actionMutation.isPending}
                >
                  <ExternalLink className="w-4 h-4 mr-1" />
                  启动
                </Button>
              </div>

              <div className="pt-2 flex gap-3">
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => handleAction('lock_screen')}
                  className="flex items-center gap-1"
                >
                  <Lock className="w-4 h-4" />
                  立即锁屏电脑
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>

        <div className="space-y-6">
          <Card className="shadow-sm">
            <CardHeader>
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle className="flex items-center gap-2">
                    <Camera className="w-5 h-5 text-teal-500" />
                    魔理沙视线快照 (Screen Vision)
                  </CardTitle>
                  <CardDescription>查看桌面当前画面，支持远程看屏。</CardDescription>
                </div>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => refetchScreenshot()}
                  disabled={screenshotLoading}
                  className="flex items-center gap-1.5"
                >
                  <Camera className={`w-4 h-4 ${screenshotLoading ? 'animate-spin' : ''}`} />
                  立即截屏
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              {screenshot?.image_base64 ? (
                <div className="rounded-lg overflow-hidden border bg-muted/30">
                  <img
                    src={screenshot.image_base64}
                    alt="桌面快照"
                    className="w-full h-auto max-h-[380px] object-contain mx-auto"
                  />
                  <div className="p-2 text-right text-xs text-muted-foreground border-t bg-muted/10">
                    截取时间：{new Date(screenshot.timestamp * 1000).toLocaleTimeString()}
                  </div>
                </div>
              ) : (
                <div className="h-[280px] rounded-lg border-2 border-dashed flex flex-col items-center justify-center text-muted-foreground space-y-2">
                  <Camera className="w-10 h-10 stroke-1 opacity-50" />
                  <p className="text-sm">点击右上角「立即截屏」获取当前屏幕实时画面</p>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}
