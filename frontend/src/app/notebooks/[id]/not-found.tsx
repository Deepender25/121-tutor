import { NotFoundState } from "@/components/RouteStates";

export const metadata = { title: "Notebook not found" };

export default function NotebookNotFound() {
  return <NotFoundState title="Notebook not found" body="It may have been deleted. Your other notebooks are still here." />;
}
