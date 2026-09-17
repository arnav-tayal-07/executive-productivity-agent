export const PEOPLE_NAMES = {
  "arjun.malhotra@veridian-corp.example": "Arjun",
  "neha.kapoor@veridian-corp.example": "Neha",
  "raghav.sethi@veridian-corp.example": "Raghav",
  "divya.rao@veridian-corp.example": "Divya",
  "priya.nair@meridianlogistics.example": "Priya",
  "facilities@veridian-corp.example": "Facilities",
  unclear: "Unclear",
};

export const label = (email) => PEOPLE_NAMES[email] || email;
