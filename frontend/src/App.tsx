import { Navigate, Route, Routes } from "react-router-dom";

import ProjectsPage from "@/pages/ProjectsPage";
import WorkbenchPage from "@/pages/WorkbenchPage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<ProjectsPage />} />
      <Route path="/projects/:projectId" element={<WorkbenchPage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
