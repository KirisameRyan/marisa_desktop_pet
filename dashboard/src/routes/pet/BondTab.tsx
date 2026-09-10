import { useQuery } from '@tanstack/react-query'
import { BookOpen, ExternalLink, Heart, Link2, Sparkles } from 'lucide-react'
import { Link } from '@tanstack/react-router'
import { fetchPetFavorability } from '@/lib/pet-api'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { Badge } from '@/components/ui/badge'

/** 记忆与羁绊：好感度、跨端记忆与主人画像入口。 */
export function BondTab() {
  const { data: favor, isLoading } = useQuery({
    queryKey: ['pet-favorability'],
    queryFn: fetchPetFavorability,
    refetchInterval: 30000
  })

  const value = favor?.favorability ?? 0

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <Card className="shadow-sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Heart className="w-5 h-5 text-rose-500" />
            好感度与羁绊
          </CardTitle>
          <CardDescription>
            桌面与 QQ 共用同一份好感度（data/marisa/favorability.json），
            聊天、工具与巡检都会让它慢慢涨。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {isLoading ? (
            <p className="text-sm text-muted-foreground">读取中…</p>
          ) : (
            <>
              <div className="flex items-baseline gap-3">
                <span className="text-4xl font-bold text-rose-600 dark:text-rose-400">{value}</span>
                <span className="text-sm text-muted-foreground">/ 100</span>
                <Badge variant="outline" className="ml-auto">
                  {favor?.rank || '初识的魔法使'}
                </Badge>
              </div>
              <Progress value={value} className="h-2.5" />
              <p className="text-xs text-muted-foreground">
                累计互动 {favor?.total_interactions ?? 0} 次
                {favor?.updated_at ? ` · 最近更新 ${favor.updated_at}` : ''}
              </p>
            </>
          )}
        </CardContent>
      </Card>

      <Card className="shadow-sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <BookOpen className="w-5 h-5 text-indigo-500" />
            主人的长期记忆
          </CardTitle>
          <CardDescription>
            魔理沙对主人的记忆统一存放在长期记忆系统里，包含人物画像、事实与聊天摘要，
            桌面聊天与 QQ 私聊共享同一套记忆空间。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Button variant="outline" size="sm" className="gap-1.5" asChild>
            <Link to="/resource/knowledge-base">
              <ExternalLink className="w-4 h-4" />
              查看与修正长期记忆
            </Link>
          </Button>
          <Button variant="ghost" size="sm" className="gap-1.5" asChild>
            <Link to="/chat-management">
              <Link2 className="w-4 h-4" />
              管理聊天流与记忆共享组
            </Link>
          </Button>
          <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
            <Sparkles className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            想让桌面聊天与某段 QQ 私聊互相参考记忆，可在聊天流管理里把两者放进同一个共享记忆组。
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
