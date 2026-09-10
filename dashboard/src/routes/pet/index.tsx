import { Sparkles } from 'lucide-react'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { Badge } from '@/components/ui/badge'
import { ConsoleTab } from './ConsoleTab'
import { BehaviorTab } from './BehaviorTab'
import { VoiceTab } from './VoiceTab'
import { BondTab } from './BondTab'

/** 魔理沙桌宠：控制台、行为、语音与羁绊的唯一入口。 */
export function PetConsolePage() {
  return (
    <div className="container mx-auto p-6 space-y-6 max-w-6xl">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b pb-4">
        <div>
          <div className="flex items-center gap-2">
            <Sparkles className="w-6 h-6 text-yellow-500 animate-pulse" />
            <h1 className="text-2xl font-bold tracking-tight">魔理沙桌宠 (Marisa Desktop Pet)</h1>
            <Badge variant="outline" className="text-xs bg-yellow-500/10 text-yellow-600 border-yellow-500/30">
              原生一体化直控
            </Badge>
          </div>
          <p className="text-sm text-muted-foreground mt-1">
            桌面上这只魔理沙的全部设置与遥控都在这里：行为、语音、羁绊一页搞定，
            与 QQ 共用同一颗大脑。
          </p>
        </div>
      </div>

      <Tabs defaultValue="console" className="space-y-4">
        <TabsList>
          <TabsTrigger value="console">控制台</TabsTrigger>
          <TabsTrigger value="behavior">桌宠行为</TabsTrigger>
          <TabsTrigger value="voice">语音与音乐</TabsTrigger>
          <TabsTrigger value="bond">记忆与羁绊</TabsTrigger>
        </TabsList>

        <TabsContent value="console">
          <ConsoleTab />
        </TabsContent>
        <TabsContent value="behavior">
          <BehaviorTab />
        </TabsContent>
        <TabsContent value="voice">
          <VoiceTab />
        </TabsContent>
        <TabsContent value="bond">
          <BondTab />
        </TabsContent>
      </Tabs>
    </div>
  )
}
