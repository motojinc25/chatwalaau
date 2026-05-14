import { Calendar, Cloud, Droplets, MapPin, Thermometer, Wind } from 'lucide-react'
import { useMemo } from 'react'
import type { ToolCall } from '@/types/chat'

interface WeatherData {
  location: string
  conditions: string
  temperature: string
  feelsLike: string
  humidity: string
  wind: string
  precipitation: string
  dataTime: string
}

interface ForecastDay {
  date: string
  conditions: string
  tempMax: string
  tempMin: string
  precipitation: string
  precipitationProbability: string
}

interface ForecastData {
  location: string
  timezone: string
  days: ForecastDay[]
}

interface LocationData {
  name: string
  country: string
  region: string
  coordinates: {
    latitude: number
    longitude: number
  }
}

const conditionIcons: Record<string, string> = {
  'Clear sky': '☀️',
  'Mainly clear': '🌤️',
  'Partly cloudy': '⛅',
  Overcast: '☁️',
  Foggy: '🌫️',
  'Depositing rime fog': '🌫️',
  'Light drizzle': '🌦️',
  'Moderate drizzle': '🌦️',
  'Dense drizzle': '🌧️',
  'Slight rain': '🌧️',
  'Moderate rain': '🌧️',
  'Heavy rain': '🌧️',
  'Light freezing rain': '🌨️',
  'Heavy freezing rain': '🌨️',
  'Slight snow fall': '🌨️',
  'Moderate snow fall': '❄️',
  'Heavy snow fall': '❄️',
  'Snow grains': '❄️',
  'Slight rain showers': '🌦️',
  'Moderate rain showers': '🌧️',
  'Violent rain showers': '⛈️',
  'Slight snow showers': '🌨️',
  'Heavy snow showers': '❄️',
  Thunderstorm: '⛈️',
  'Thunderstorm with slight hail': '⛈️',
  'Thunderstorm with heavy hail': '⛈️',
}

function getConditionIcon(condition: string): string {
  return conditionIcons[condition] ?? '🌡️'
}

function CurrentWeatherCard({ data }: { data: WeatherData }) {
  return (
    <div className="my-2 w-full max-w-md rounded-xl border border-blue-200/50 bg-gradient-to-br from-blue-50 to-sky-50 p-4 shadow-sm dark:border-blue-800/50 dark:from-blue-950/30 dark:to-sky-950/30">
      <div className="mb-3 flex items-center gap-2 text-base font-semibold">
        <MapPin className="h-4 w-4 text-blue-600 dark:text-blue-400" />
        {data.location}
      </div>

      <div className="mb-3 flex items-center gap-4">
        <span className="text-4xl">{getConditionIcon(data.conditions)}</span>
        <div>
          <div className="text-3xl font-bold tracking-tight">{data.temperature}</div>
          <div className="text-sm text-muted-foreground">{data.conditions}</div>
        </div>
      </div>

      <div className="mb-2 grid grid-cols-2 gap-2 text-sm">
        <div className="flex items-center gap-1.5 text-muted-foreground">
          <Thermometer className="h-3.5 w-3.5" />
          <span>Feels like {data.feelsLike}</span>
        </div>
        <div className="flex items-center gap-1.5 text-muted-foreground">
          <Droplets className="h-3.5 w-3.5" />
          <span>Humidity {data.humidity}</span>
        </div>
        <div className="flex items-center gap-1.5 text-muted-foreground">
          <Wind className="h-3.5 w-3.5" />
          <span>{data.wind}</span>
        </div>
        <div className="flex items-center gap-1.5 text-muted-foreground">
          <Cloud className="h-3.5 w-3.5" />
          <span>Precip {data.precipitation}</span>
        </div>
      </div>

      <div className="text-xs text-muted-foreground/70">{data.dataTime}</div>
    </div>
  )
}

function ForecastCard({ data }: { data: ForecastData }) {
  return (
    <div className="my-2 w-full max-w-lg rounded-xl border border-indigo-200/50 bg-gradient-to-br from-indigo-50 to-purple-50 p-4 shadow-sm dark:border-indigo-800/50 dark:from-indigo-950/30 dark:to-purple-950/30">
      <div className="mb-3 flex items-center gap-2 text-base font-semibold">
        <Calendar className="h-4 w-4 text-indigo-600 dark:text-indigo-400" />
        {data.location}
      </div>

      <div className="space-y-1.5">
        {data.days.map((day) => (
          <div
            key={day.date}
            className="flex items-center gap-3 rounded-md px-2 py-1.5 text-sm hover:bg-white/50 dark:hover:bg-white/5">
            <span className="w-20 shrink-0 font-medium">{formatDate(day.date)}</span>
            <span className="w-6 text-center">{getConditionIcon(day.conditions)}</span>
            <span className="w-24 shrink-0 text-muted-foreground">
              {day.tempMax} / {day.tempMin}
            </span>
            <span className="flex items-center gap-1 text-muted-foreground">
              <Droplets className="h-3 w-3" />
              {day.precipitationProbability}
            </span>
          </div>
        ))}
      </div>
      <div className="mt-2 text-xs text-muted-foreground/70">Timezone: {data.timezone}</div>
    </div>
  )
}

function LocationCard({ data }: { data: LocationData }) {
  return (
    <div className="my-2 w-full max-w-md rounded-xl border border-green-200/50 bg-gradient-to-br from-green-50 to-emerald-50 px-4 py-3 shadow-sm dark:border-green-800/50 dark:from-green-950/30 dark:to-emerald-950/30">
      <div className="flex items-center gap-3">
        <MapPin className="h-5 w-5 text-green-600 dark:text-green-400" />
        <div>
          <div className="font-medium">
            {data.name}, {data.country}
          </div>
          <div className="text-xs text-muted-foreground">
            {data.region} ({data.coordinates.latitude.toFixed(4)}, {data.coordinates.longitude.toFixed(4)})
          </div>
        </div>
      </div>
    </div>
  )
}

function formatDate(dateStr: string): string {
  const date = new Date(dateStr)
  return date.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })
}

const WEATHER_TOOLS = new Set(['get_coords_by_city', 'get_current_weather_by_coords', 'get_weather_next_week'])

export function WeatherToolResults({ toolCalls }: { toolCalls: ToolCall[] }) {
  const weatherResults = useMemo(
    () => toolCalls.filter((tc) => WEATHER_TOOLS.has(tc.name) && tc.status === 'completed' && tc.result),
    [toolCalls],
  )

  if (weatherResults.length === 0) return null

  return (
    <div className="flex flex-col gap-1">
      {weatherResults.map((tc) => (
        <WeatherResultCard key={tc.id} toolCall={tc} />
      ))}
    </div>
  )
}

function WeatherResultCard({ toolCall }: { toolCall: ToolCall }) {
  const parsed = useMemo(() => {
    try {
      return JSON.parse(toolCall.result ?? '')
    } catch {
      return null
    }
  }, [toolCall.result])

  if (!parsed || parsed.error) return null

  if (toolCall.name === 'get_coords_by_city' && parsed.name) {
    return <LocationCard data={parsed as LocationData} />
  }

  if (toolCall.name === 'get_current_weather_by_coords' && parsed.temperature) {
    return <CurrentWeatherCard data={parsed as WeatherData} />
  }

  if (toolCall.name === 'get_weather_next_week' && parsed.days) {
    return <ForecastCard data={parsed as ForecastData} />
  }

  return null
}
