import { NotFoundState } from "@/components/RouteStates";

export const metadata = { title: "Not found" };

export default function NotFound() {
  return <NotFoundState title="Page not found" body="There's nothing at this address." />;
}
