import { useState } from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { RouterProvider } from "react-router"
import { TooltipProvider } from "@/components/ui/tooltip"
import { createRouter } from "./router"

export function makeQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { refetchOnWindowFocus: false }, mutations: { retry: false } } })
}

export default function App() {
  const [qc] = useState(makeQueryClient)
  const [router] = useState(createRouter)
  return (
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={300}>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>
  )
}
