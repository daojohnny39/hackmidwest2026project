// Replace these five placeholders with the team's approved names and roles.
export const team = Array.from({ length: 5 }, (_, index) => ({
  id: `developer-${index + 1}`,
  name: `Developer ${String(index + 1).padStart(2, '0')}`,
  role: 'Details coming soon',
}))
