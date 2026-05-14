import { Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'

export function NotFoundPage() {
  return (
    <div className="flex h-screen flex-col items-center justify-center gap-4">
      <h1 className="text-4xl font-bold">404</h1>
      <p className="text-muted-foreground">Page Not Found</p>
      <Button asChild>
        <Link to="/chat">Go to Chat</Link>
      </Button>
    </div>
  )
}
