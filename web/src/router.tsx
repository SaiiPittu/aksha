import { createBrowserRouter, Navigate, type RouteObject } from "react-router"
import { AcceptInvite } from "@/app/auth/AcceptInvite"
import { CreateOrg } from "@/app/auth/CreateOrg"
import { AppGate, NotFound, PublicGate } from "@/app/auth/Gates"
import { SignIn } from "@/app/auth/SignIn"
import { TryAksha } from "@/app/auth/TryAksha"
import { ChatRoute } from "@/app/chat/ChatRoute"
import { EvalsPage, FilesPage, InboxPage, SessionsPage } from "@/app/pages/Pages"
import { AuditPage } from "@/app/settings/AuditPage"
import { InvitesPage } from "@/app/settings/InvitesPage"
import { MembersPage } from "@/app/settings/MembersPage"
import { ProfilePage } from "@/app/settings/ProfilePage"
import { RuntimePage } from "@/app/settings/RuntimePage"
import { TokensPage } from "@/app/settings/TokensPage"

/** Every route of the web app (library mode: the guard is a layout element, not a loader). */
export const routes: RouteObject[] = [
  { element: <PublicGate page="sign-in" />, children: [{ path: "/sign-in", element: <SignIn /> }] },
  { element: <PublicGate page="sign-up" />, children: [{ path: "/sign-up", element: <CreateOrg /> }] },
  { element: <PublicGate page="try" />, children: [{ path: "/try", element: <TryAksha /> }] },
  { path: "/invite/:token", element: <AcceptInvite /> },
  {
    element: <AppGate />,
    children: [
      { path: "/", element: <ChatRoute /> },
      { path: "/s/:session", element: <ChatRoute /> },
      { path: "/inbox", element: <InboxPage /> },
      { path: "/sessions", element: <SessionsPage /> },
      { path: "/files", element: <FilesPage /> },
      { path: "/evals", element: <EvalsPage /> },
      { path: "/settings", element: <Navigate to="/settings/profile" replace /> },
      { path: "/settings/profile", element: <ProfilePage /> },
      { path: "/settings/members", element: <MembersPage /> },
      { path: "/settings/invites", element: <InvitesPage /> },
      { path: "/settings/tokens", element: <TokensPage /> },
      { path: "/settings/audit", element: <AuditPage /> },
      { path: "/settings/runtime", element: <RuntimePage /> },
    ],
  },
  { path: "*", element: <NotFound /> },
]

export const createRouter = () => createBrowserRouter(routes)
